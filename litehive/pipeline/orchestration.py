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

from litehive.config import load_config
from litehive.config.engine_models import resolve_task_retry_policy
from litehive.models import TaskRecord
from litehive.models.runtime_models import RuntimeHookRejectFingerprint
from litehive.tasks.crud import get_task, get_task_worktree_path, save_task
from litehive.workspace.locking import runner_heartbeat, workspace_runner_guard

from .agents.base import PromptContext
from .engines import ConfigBackedEngineSelector, EngineFactory
from .heru_factory import heru_engine_factory
from .journal import SqliteJournal
from .nodes import (
    GitCommitNode,
    GitWorktreeSyncNode,
    HookSpec,
    PreExecRecoveryNode,
    ReadyNode,
    SubprocessHookRunner,
)
from .nodes.system import CommitNode
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


def _sync_back(state: TaskState, workspace_root: Path) -> TaskRecord | None:
    """Mirror the pipeline stage back to the TaskRecord so litehive status stays accurate."""
    task_record = get_task(workspace_root, state.task_id)
    if task_record is None:
        return None
    task_record.runtime.consecutive_same_hook_rejects = state.consecutive_same_hook_rejects
    task_record.runtime.last_hook_reject_fingerprint = (
        None
        if state.last_hook_reject_fingerprint is None
        else RuntimeHookRejectFingerprint(
            point=state.last_hook_reject_fingerprint.point,
            command=state.last_hook_reject_fingerprint.command,
            description=state.last_hook_reject_fingerprint.description,
            fingerprint=state.last_hook_reject_fingerprint.fingerprint,
        )
    )
    task_record.runtime.hook_reject_recovery_invoked = state.hook_reject_recovery_invoked
    if state.stage == "done":
        task_record.status = "done"
        task_record.pipeline_status = "done"
    elif state.stage == "failed":
        commit_stages = {"commit", "before_commit", "after_commit", "merge_resolving"}
        if state.origin_stage in commit_stages:
            task_record.status = "merge_failed"
            task_record.pipeline_status = "merge_failed"
        else:
            task_record.status = "flagged"
            task_record.pipeline_status = "flagged"
            if state.failure_context.get("reason_code") == "hook_reject_loop":
                task_record.flag_reason = "hook_reject_loop"
    else:
        task_record.status = "in_progress"
        task_record.pipeline_status = _STAGE_TO_PIPELINE_STATUS.get(state.stage, task_record.pipeline_status)
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
    from litehive.tasks.crud import get_task as _get_task
    from litehive.tasks.worktrees import resolve_recorded_worktree_path

    task = _get_task(root, state.task_id)
    if task is None:
        return root
    wt = get_task_worktree_path(task)
    if not wt:
        return root
    return resolve_recorded_worktree_path(root, wt) or root


def _build_commit_node(root: Path) -> CommitNode:
    """Return the production ``GitCommitNode`` bound to this workspace."""
    return GitCommitNode(root, worktree_resolver=lambda state: _resolve_worktree(root, state))


def _build_worktree_sync_node(root: Path) -> GitWorktreeSyncNode:
    """Return the production ``GitWorktreeSyncNode`` bound to this workspace."""
    return GitWorktreeSyncNode(
        worktree_resolver=lambda state: _resolve_worktree(root, state),
    )


def _missing_worktree_probe(root: Path):
    """Return a probe callable that flags tasks whose worktree_path is gone."""

    from litehive.tasks.crud import get_task as _get_task
    from litehive.tasks.worktrees import resolve_recorded_worktree_path

    def _probe(state) -> bool:
        task = _get_task(root, state.task_id)
        if task is None:
            return False
        wt = get_task_worktree_path(task)
        if not wt:
            return False
        path = resolve_recorded_worktree_path(root, wt)
        if path is None:
            return False
        return not path.exists()

    return _probe


def _clear_stale_worktree_repair(root: Path):
    """Return a repair callable that clears a stale worktree_path on the task."""

    from litehive.tasks.crud import get_task as _get_task
    from litehive.tasks.crud import set_task_worktree_path, save_task
    from litehive.tasks.worktrees import resolve_recorded_worktree_path

    def _repair(state) -> None:
        task = _get_task(root, state.task_id)
        if task is None:
            return
        wt = get_task_worktree_path(task)
        if not wt:
            return
        path = resolve_recorded_worktree_path(root, wt)
        if path is not None and path.exists():
            return
        set_task_worktree_path(task, None)
        save_task(root, task)

    return _repair


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
        selector = ConfigBackedEngineSelector(config, factory)
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
        retry_budget, _retry_source = resolve_task_retry_policy(task, config)

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
        )

        # 3. Run under the heartbeat so `litehive status` sees the active task.
        with runner_heartbeat(root, active_task_id=task.id):
            final_state = runner.run_task(task.id)

        # 4. Mirror terminal state back to the v1 TaskRecord.
        updated_task = _sync_back(final_state, root) or task

    return ExecutionResult(
        task=updated_task,
        final_state=final_state,
        final_stage=final_state.stage,
        failed_reason=final_state.failed_reason,
        failed_message=final_state.failed_message,
    )
