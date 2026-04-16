"""v2 task orchestration entry point.

One function — ``run_task(root, task)`` — that wires up the pipeline
end-to-end and drives one task through the state machine. It is the



What it does, in order:

1. Loads workspace config.
2. Initializes (or loads) the ``TaskState`` via the bridge so the
   sqlite row exists with the right pipeline mode.
3. Constructs the engine selector / session store / persistence /
   journal / hook runner / commit node.
4. Builds the full ``NodeRegistry``.
5. Runs ``StateMachineRunner.run_task(task_id)``.
6. Syncs the v2 terminal state back to the v1 ``TaskRecord`` so
   ``litehive status`` and the queue stay coherent.

Returns a small ``ExecutionResult`` named-tuple-ish dataclass the
caller can render.
"""

from dataclasses import dataclass
from pathlib import Path
import subprocess

from litehive.config.loading import load_config
from litehive.config.engine_models import resolve_task_retry_policy
from litehive.git.ops import GitError, remove_worktree
from litehive.domain.task import TaskRecord
from litehive.domain.runtime import RuntimeHookRejectFingerprint
from litehive.domain.common import utcnow
from litehive.state.records import (
    clear_task_worktree_path,
    get_task,
    get_task_worktree_path,
    save_task,
    set_task_commit_sha,
)
from litehive.tasks.worktrees import resolve_recorded_worktree_path, task_worktree_branch
from litehive.state.locking import persist_future_task_update
from litehive.state.locking import runner_heartbeat, workspace_runner_guard

from litehive.roles.base import PromptContext
from .engines import ConfigBackedEngineSelector, EngineFactory
from .heru_factory import heru_engine_factory
from .journal import SqliteJournal
from .nodes.hook import HookSpec, SubprocessHookRunner
from .nodes.system import (
    CommitNode,
    GitCommitNode,
    GitWorktreeSyncNode,
    PreExecRecoveryNode,
    ReadyNode,
)
from .persistence import SqlitePersistence, TaskState
from .registry import build_registry
from .runner import StateMachineRunner
from .sessions import SqliteSessionStore
from .types import PipelineMode as _PipelineMode


def _load_or_initialize(task_id: str, workspace_root: Path, persistence: SqlitePersistence) -> TaskState:
    """Return a ``TaskState`` for ``task_id``, creating the row if needed."""
    task_record = get_task(workspace_root, task_id)
    if task_record is None:
        raise LookupError(f"no task record for {task_id!r}")
    raw = task_record.pipeline_mode
    mode = _PipelineMode(raw) if isinstance(raw, str) and raw else _PipelineMode.FULL
    return persistence.initialize(task_id, pipeline_mode=mode)


_STAGE_TO_PIPELINE_STATUS: dict[str, str] = {
    "ready": "backlog", "recovering_pre_exec": "backlog",
    "before_grooming": "grooming", "grooming": "grooming", "after_grooming": "grooming",
    "before_implementing": "implementing", "implementing": "implementing", "after_implementing": "implementing",
    "before_testing": "testing", "testing": "testing", "after_testing": "testing",
    "before_accepting": "accepting", "accepting": "accepting", "after_accepting": "accepting",
    "before_commit": "commit_to_git", "commit": "commit_to_git", "after_commit": "commit_to_git",
    "merge_resolving": "commit_to_git", "recovering": "grooming",
}


def _runtime_hook_reject_fingerprint(state: TaskState) -> RuntimeHookRejectFingerprint | None:
    fingerprint = state.last_hook_reject_fingerprint
    if fingerprint is None:
        return None
    return RuntimeHookRejectFingerprint(
        point=fingerprint.point,
        command=fingerprint.command,
        description=fingerprint.description,
        fingerprint=fingerprint.fingerprint,
    )


def _sync_runtime_fields(task_record: TaskRecord, state: TaskState) -> None:
    now = utcnow()
    task_record.runtime.consecutive_same_hook_rejects = state.consecutive_same_hook_rejects
    task_record.runtime.last_hook_reject_fingerprint = _runtime_hook_reject_fingerprint(state)
    task_record.runtime.hook_reject_recovery_invoked = state.hook_reject_recovery_invoked
    if state.stage in {"done", "failed"}:
        task_record.runtime.execution_status = "idle"
        task_record.runtime.current_stage = task_record.runtime.current_stage.model_copy(
            update={
                "stage": None,
                "status": "idle",
                "started_at": None,
                "completed_at": None,
                "updated_at": now,
            }
        )
        return
    current_stage = task_record.runtime.current_stage
    started_at = current_stage.started_at if current_stage.stage == state.stage else now
    task_record.runtime.execution_status = "running"
    task_record.runtime.current_stage = current_stage.model_copy(
        update={
            "stage": state.stage,
            "status": "running",
            "started_at": started_at,
            "completed_at": None,
            "updated_at": now,
        }
    )


def _latest_recovery_trigger(state: TaskState):
    if state.active_recovery_trigger is not None:
        return state.active_recovery_trigger
    if state.recovery_history:
        return state.recovery_history[-1].trigger
    return None


def _sync_terminal_status(task_record: TaskRecord, state: TaskState) -> str | None:
    journal_message: str | None = None
    commit_result = state.commit_result
    if state.stage == "done":
        task_record.status = "done"
        task_record.pipeline_status = "done"
        if commit_result is not None:
            set_task_commit_sha(task_record, commit_result.head_sha)
            if commit_result.reason == "already_landed":
                journal_message = (
                    f"commit_to_git reconciled: worktree patch already landed on main at {commit_result.head_sha}."
                )
            else:
                journal_message = (
                    f"commit_to_git reconciled as a no-op on main at {commit_result.head_sha}; "
                    "no new integration commit was needed."
                )
    elif state.stage == "failed":
        trigger = _latest_recovery_trigger(state)
        origin_stage = trigger.origin_stage if trigger is not None else None
        failed_reason = state.failed_reason.value if hasattr(state.failed_reason, "value") else state.failed_reason
        if origin_stage == "merge_resolving":
            task_record.status = "merge_failed"
            task_record.pipeline_status = "merge_failed"
            if state.failed_message:
                journal_message = f"commit_to_git failed during merge reconciliation: {state.failed_message}"
        else:
            task_record.status = "flagged"
            task_record.pipeline_status = "flagged"
            if trigger is not None and trigger.reason_code == "hook_reject_loop":
                task_record.flag_reason = "hook_reject_loop"
            elif failed_reason == "recovery_budget_hit":
                trigger_kind = trigger.trigger_event_kind.value if trigger is not None else None
                task_record.flag_reason = (
                    "crash_budget_exhausted"
                    if trigger_kind in {"crash", "timeout"}
                    else "recovery_budget_exhausted"
                )
    else:
        task_record.status = "in_progress"
        task_record.pipeline_status = _STAGE_TO_PIPELINE_STATUS.get(state.stage, task_record.pipeline_status)
    return journal_message


def _sync_back(state: TaskState, workspace_root: Path) -> TaskRecord | None:
    """Mirror the pipeline stage back to the TaskRecord so litehive status stays accurate."""
    task_record = get_task(workspace_root, state.task_id)
    if task_record is None:
        return None
    _sync_runtime_fields(task_record, state)
    journal_message = _sync_terminal_status(task_record, state)
    if journal_message is not None:
        persist_future_task_update(workspace_root, task_record, journal_message=journal_message)
    else:
        save_task(workspace_root, task_record)
    return task_record


@dataclass
class ExecutionResult:
    """Result of running one task through the pipeline state machine."""

    task: TaskRecord | None
    final_state: TaskState | None
    final_stage: str
    failed_reason: str | None = None
    failed_message: str | None = None


def _resolve_worktree(root: Path, state: TaskState) -> Path:
    """Look up the on-disk worktree path for a task, falling back to root."""
    _, worktree_path = _task_recorded_worktree(root, state.task_id)
    return worktree_path or root


def _task_recorded_worktree(root: Path, task_id: str) -> tuple[TaskRecord | None, Path | None]:
    task = get_task(root, task_id)
    if task is None:
        return None, None
    recorded = get_task_worktree_path(task)
    if not recorded:
        return task, None
    return task, resolve_recorded_worktree_path(root, recorded)


def _build_commit_node(root: Path) -> CommitNode:
    """Return the production ``GitCommitNode`` bound to this workspace."""
    return GitCommitNode(root, worktree_resolver=lambda state: _resolve_worktree(root, state))


def _build_worktree_sync_node(root: Path) -> GitWorktreeSyncNode:
    """Return the production ``GitWorktreeSyncNode`` bound to this workspace."""
    return GitWorktreeSyncNode(
        workspace_root=root,
        worktree_resolver=lambda state: _resolve_worktree(root, state),
    )


def _missing_worktree_probe(root: Path):
    """Return a probe callable that flags tasks whose worktree_path is gone."""

    def _probe(state) -> bool:
        task, path = _task_recorded_worktree(root, state.task_id)
        if task is None or path is None:
            return False
        return not path.exists()

    return _probe


def _clear_stale_worktree_repair(root: Path):
    """Return a repair callable that clears a stale worktree_path on the task."""

    def _repair(state) -> None:
        task, path = _task_recorded_worktree(root, state.task_id)
        if task is None or (path is not None and path.exists()):
            return
        clear_task_worktree_path(task)
        save_task(root, task)

    return _repair


def _cleanup_terminal_worktree(root: Path, task: TaskRecord | None) -> None:
    if task is None:
        return
    worktree_rel = get_task_worktree_path(task)
    if not worktree_rel:
        return
    worktree_path = resolve_recorded_worktree_path(root, worktree_rel)
    if worktree_path is not None and worktree_path.exists():
        remove_worktree(root, worktree_path, force=True)
    clear_task_worktree_path(task)
    save_task(root, task)
    branch = task_worktree_branch(task)
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def hook_specs_from_config(config) -> dict[str, list[HookSpec]]:
    """Translate ``LitehiveConfig.runner_hooks`` into ``HookSpec`` lists.

    Config stores runner hooks as ``dict[phase_name, list[HookConfig]]``
    where HookConfig has ``command``, ``reject_on_failure``,
    ``timeout_seconds``, ``description``, ``instructions_on_failure``. v2
    HookSpec is a strict subset — just command / reject_on_failure /
    timeout_seconds. The phase names match (``before_grooming``,
    ``after_implementing``, …), so this is a straight per-phase rewrite.
    """
    out: dict[str, list[HookSpec]] = {}
    raw = getattr(config, "runner_hooks", None) or {}
    for phase, hooks in raw.items():
        specs: list[HookSpec] = []
        for hook in hooks or []:
            specs.append(
                HookSpec(
                    command=hook.command,
                    reject_on_failure=bool(hook.reject_on_failure),
                    timeout_seconds=int(hook.timeout_seconds or 60),
                    description=hook.description,
                )
            )
        if specs:
            out[phase] = specs
    return out


def run_task(
    root: Path,
    task: TaskRecord,
    *,
    engine_factory: EngineFactory | None = None,
    engine_override: str | None = None,
    model_override: str | None = None,
) -> ExecutionResult:
    """Run a single task through the state machine.

    Takes the workspace runner guard and publishes a heartbeat so other
    tools see the task as active. Always uses the real ``GitCommitNode``
    — 

    ``engine_factory`` is an injection point for tests: pass a callable
    that produces fake ``Engine`` instances and the pipeline will use it in place
    of the real ``heru_engine_factory``.
    """
    root = root.resolve()
    config = load_config(root)

    with workspace_runner_guard(root):
        persistence = SqlitePersistence(root)
        _load_or_initialize(task.id, root, persistence)

        factory = engine_factory or heru_engine_factory(root)
        selector = ConfigBackedEngineSelector(
            config,
            factory,
            workspace_root=root,
            engine_override=engine_override,
            model_override=model_override,
        )
        sessions = SqliteSessionStore(root)
        journal = SqliteJournal(root)
        hook_runner = SubprocessHookRunner(root)
        commit_node = _build_commit_node(root)
        worktree_sync_node = _build_worktree_sync_node(root)
        ready_node = ReadyNode(probes=[_missing_worktree_probe(root)])
        pre_exec_recovery_node = PreExecRecoveryNode(
            repairs=[_clear_stale_worktree_repair(root)],
        )
        prompt_context = PromptContext(workspace_root=root)
        hook_specs = hook_specs_from_config(config)
        retry_budget = resolve_task_retry_policy(task, config)

        registry = build_registry(
            selector=selector,
            session_store=sessions,
            hook_runner=hook_runner,
            commit_node=commit_node,
            worktree_sync_node=worktree_sync_node,
            ready_node=ready_node,
            pre_exec_recovery_node=pre_exec_recovery_node,
            prompt_context=prompt_context,
            hook_specs=hook_specs,
            retry_budget=retry_budget,
            retry_on=tuple(config.retry_on),
        )

        runner = StateMachineRunner(
            registry,
            persistence,
            journal=journal,
            state_sync=lambda state: _sync_back(state, root),
        )

        # 3. Run under the heartbeat so `litehive status` sees the active task.
        with runner_heartbeat(root, active_task_id=task.id):
            final_state = runner.run_task(task.id)

        # 4. Mirror terminal state back to the v1 TaskRecord.
        updated_task = _sync_back(final_state, root) or task
        if final_state.stage in {"done", "failed"}:
            try:
                _cleanup_terminal_worktree(root, updated_task)
            except GitError:
                pass

    return ExecutionResult(
        task=updated_task,
        final_state=final_state,
        final_stage=final_state.stage,
        failed_reason=final_state.failed_reason,
        failed_message=final_state.failed_message,
    )
