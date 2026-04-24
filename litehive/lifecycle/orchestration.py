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

from dataclasses import dataclass, replace
from pathlib import Path
import subprocess

from litehive.config.loading import load_config
from litehive.config.engine_models import resolve_task_rejection_loop_limit, resolve_task_retry_policy
from litehive.git.ops import GitError, remove_worktree
from litehive.domain.reports import StageReport, TaskActivityEntry
from litehive.domain.task import TaskRecord
from litehive.domain.runtime import RuntimeHookRejectFingerprint, RuntimeRecoveryOutcome
from litehive.domain.common import cap_feedback, utcnow
from litehive.state.records import (
    clear_task_worktree_path,
    get_task,
    get_task_worktree_path,
    save_task,
    set_task_commit_sha,
)
from litehive.worktree import resolve_recorded_worktree_path, task_worktree_branch
from litehive.tasks.activity import append_task_activity, latest_task_activity_entry
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.journal import append_journal
from litehive.tasks.reports import record_stage_report
from litehive.tasks.runtime import apply_task_outcome
from litehive.state.locking import persist_future_task_update
from litehive.state.locking import runner_heartbeat, workspace_runner_guard

from litehive.roles.base import PromptContext
from .events import HookOk, Reject
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
from .persistence import Limits, SqlitePersistence, TaskNotFound, TaskState
from .registry import build_registry
from .runner import StateMachineRunner
from .sessions import SqliteSessionStore
from .transitions import Transition
from .types import PipelineMode as _PipelineMode


def _load_or_initialize(task_id: str, workspace_root: Path, persistence: SqlitePersistence) -> TaskState:
    """Return a ``TaskState`` for ``task_id``, creating the row if needed."""
    task_record = get_task(workspace_root, task_id)
    if task_record is None:
        raise LookupError(f"no task record for {task_id!r}")
    raw = task_record.pipeline_mode
    mode = _PipelineMode(raw) if isinstance(raw, str) and raw else _PipelineMode.FULL
    entry_stage = _entry_stage_for_task(task_record)
    fresh_state_kwargs = dict(
        task_id=task_id,
        pipeline_mode=mode,
        stage="ready",
        entry_stage=entry_stage,
    )
    if entry_stage is None:
        return persistence.initialize(**fresh_state_kwargs)

    def _initialize_fresh_state() -> TaskState:
        persistence.reset(task_id)
        return persistence.initialize(**fresh_state_kwargs)

    try:
        state = persistence.load(task_id)
    except TaskNotFound:
        return persistence.initialize(**fresh_state_kwargs)
    except Exception:
        raise

    if _stale_launch_state_requires_reset(task_record, state, pipeline_mode=mode, entry_stage=entry_stage):
        return _initialize_fresh_state()
    return state


def _entry_stage_for_task(task_record: TaskRecord) -> str | None:
    stage = (
        task_record.runtime.current_stage.stage
        or (None if task_record.runtime.interruption is None else task_record.runtime.interruption.resume_stage)
        or task_record.pipeline_status
    )
    if stage in {None, "backlog", "done", "flagged", "merge_failed"}:
        return None
    if stage == "commit_to_git":
        return "commit"
    return stage


def _launch_requires_fresh_pipeline_state(task_record: TaskRecord) -> bool:
    return _entry_stage_for_task(task_record) is not None and task_record.runtime.execution_status != "running"


def _stale_launch_state_requires_reset(
    task_record: TaskRecord,
    state: TaskState,
    *,
    pipeline_mode: _PipelineMode,
    entry_stage: str,
) -> bool:
    if not _launch_requires_fresh_pipeline_state(task_record):
        return False
    if state.pipeline_mode != pipeline_mode:
        return True
    return state.stage != "ready" or state.entry_stage != entry_stage


_STAGE_TO_PIPELINE_STATUS: dict[str, str] = {
    "ready": "backlog",
    "before_grooming": "grooming",
    "grooming": "grooming",
    "after_grooming": "grooming",
    "before_implementing": "implementing",
    "implementing": "implementing",
    "after_implementing": "implementing",
    "before_testing": "testing",
    "testing": "testing",
    "after_testing": "testing",
    "before_accepting": "accepting",
    "accepting": "accepting",
    "after_accepting": "accepting",
    "commit": "commit_to_git",
    "after_commit": "commit_to_git",
    "merge_resolving": "commit_to_git",
    "recovering": "grooming",
}

_MANUAL_REVIEW_FLAG_REASONS = {
    "hook_reject_loop",
    "rejection_loop_detected",
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


def _runtime_recovery_outcome(outcome) -> RuntimeRecoveryOutcome:
    trigger = outcome.trigger
    return RuntimeRecoveryOutcome(
        origin_stage=trigger.origin_stage,
        trigger_event_kind=trigger.trigger_event_kind.value,
        fingerprint=trigger.failure_fingerprint.fingerprint,
        classification=trigger.failure_fingerprint.classification,
        budget_key=trigger.budget_key(),
        recovery_verdict=outcome.recovery_verdict,
        disposition=outcome.disposition.value,
        reason_code=outcome.reason_code,
        message=outcome.message,
        created_at=outcome.created_at,
    )


def _runtime_recovery_key(outcome: RuntimeRecoveryOutcome) -> tuple[str | None, str, str, str, str | None]:
    return (
        outcome.origin_stage,
        outcome.fingerprint,
        outcome.budget_key,
        outcome.recovery_verdict,
        outcome.created_at,
    )


def _merged_runtime_recovery_history(
    existing: list[RuntimeRecoveryOutcome],
    current_state: TaskState,
) -> list[RuntimeRecoveryOutcome]:
    merged: list[RuntimeRecoveryOutcome] = []
    seen: set[tuple[str | None, str, str, str, str | None]] = set()
    for item in [*existing, *[_runtime_recovery_outcome(outcome) for outcome in current_state.recovery_history]]:
        key = _runtime_recovery_key(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _sync_runtime_fields(task_record: TaskRecord, state: TaskState) -> None:
    now = utcnow()
    task_record.runtime.consecutive_same_hook_rejects = state.consecutive_same_hook_rejects
    task_record.runtime.last_hook_reject_fingerprint = _runtime_hook_reject_fingerprint(state)
    task_record.runtime.hook_reject_recovery_invoked = state.hook_reject_recovery_invoked
    task_record.runtime.recovery_history = _merged_runtime_recovery_history(
        task_record.runtime.recovery_history,
        state,
    )
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


def _recovery_origin_stage(origin_stage: str | None) -> str | None:
    if origin_stage in {"before_grooming", "grooming", "after_grooming", "recovering"}:
        return "grooming"
    if origin_stage in {"before_implementing", "implementing", "after_implementing"}:
        return "implementing"
    if origin_stage in {"before_testing", "testing", "after_testing"}:
        return "testing"
    if origin_stage in {"before_accepting", "accepting", "after_accepting"}:
        return "accepting"
    if origin_stage in {"before_commit", "commit", "after_commit", "merge_resolving"}:
        return "commit_to_git"
    return origin_stage


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
        merge_reject = state.last_rejection_by_stage.get("merge_resolving")
        if origin_stage == "merge_resolving" or merge_reject is not None:
            task_record.status = "merge_failed"
            task_record.pipeline_status = "merge_failed"
            if state.failed_message:
                journal_message = f"commit_to_git failed during merge reconciliation: {state.failed_message}"
        else:
            task_record.status = "flagged"
            task_record.pipeline_status = "flagged"
            origin_stage_key = _recovery_origin_stage(origin_stage)
            if failed_reason == "hook_reject_loop" or (
                trigger is not None and trigger.reason_code == "hook_reject_loop"
            ):
                task_record.flag_reason = "hook_reject_loop"
            elif failed_reason == "rejection_loop_detected":
                task_record.flag_reason = "rejection_loop_detected"
            elif failed_reason == "semantic_reject":
                task_record.flag_reason = "semantic_reject"
            elif failed_reason == "recovery_exhausted":
                task_record.flag_reason = "recovery_failed"
            elif failed_reason == "recovery_budget_hit":
                trigger_kind = trigger.trigger_event_kind.value if trigger is not None else None
                task_record.flag_reason = (
                    "crash_budget_exhausted" if trigger_kind in {"crash", "timeout"} else "recovery_budget_exhausted"
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
    before_task = snapshot_task_audit_state(task_record)
    before_last_outcome = task_record.runtime.last_outcome.model_copy(deep=True)
    _sync_runtime_fields(task_record, state)
    journal_message = _sync_terminal_status(task_record, state)
    _sync_recovery_follow_up(workspace_root, task_record, state)
    audit_entries = []
    if (
        before_task.status != task_record.status
        or before_task.pipeline_status != task_record.pipeline_status
        or before_last_outcome != task_record.runtime.last_outcome
    ):
        action = "status_changed"
        if state.stage == "failed":
            action = "failed"
        elif state.stage == "done" and task_record.status == "done":
            action = "completed"
        audit_entries.append(
            build_task_audit_entry(
                task_id=task_record.id,
                action=action,
                actor="runner",
                source="pipeline",
                before_task=before_task,
                after_task=task_record,
                context={
                    "lifecycle_stage": state.stage,
                    "failed_reason": (
                        None if state.failed_reason is None else getattr(state.failed_reason, "value", state.failed_reason)
                    ),
                    "failed_message": state.failed_message,
                },
            )
        )
    persist_future_task_update(
        workspace_root,
        task_record,
        journal_message=journal_message,
        audit_entries=audit_entries or None,
    )
    return task_record


def _sync_recovery_follow_up(root: Path, task_record: TaskRecord, state: TaskState) -> None:
    failed_reason = state.failed_reason.value if hasattr(state.failed_reason, "value") else state.failed_reason
    if state.stage != "failed":
        return
    if failed_reason != "recovery_exhausted":
        return
    latest = latest_task_activity_entry(
        root,
        task_record,
        role="recovery",
        stage="recovering",
        verdicts={"reject"},
    )
    if latest is None or not latest.follow_up_task_id:
        return
    trigger = _latest_recovery_trigger(state)
    apply_task_outcome(
        task_record,
        kind="flagged",
        stage=(trigger.origin_stage if trigger is not None else "recovering"),
        reason_code="stage_exception",
        reason=state.failed_message or latest.message or "Recovery escalated to a follow-up task.",
        retry_count=task_record.runtime.retry_count,
        retry_limit=task_record.runtime.retry_limit,
        follow_up_task_id=latest.follow_up_task_id,
        failure_classification=(None if trigger is None else trigger.failure_fingerprint.budget_key()),
        failure_diagnostics={
            "origin_stage": None if trigger is None else trigger.origin_stage,
            "trigger_event_kind": None if trigger is None else trigger.trigger_event_kind.value,
            "fingerprint": None if trigger is None else trigger.failure_fingerprint.fingerprint,
            "budget_key": None if trigger is None else trigger.budget_key(),
        },
    )


def _clear_terminal_task_from_workspace_state(root: Path, task_id: str) -> None:
    from litehive.state.persist import load_state, persist_tasks_and_state

    state = load_state(root)
    if state.active_task_id == task_id:
        state.active_task_id = None
    if task_id in state.queue:
        state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
    persist_tasks_and_state(
        root,
        tasks=(),
        state=state,
        protected_task_ids=[task_id],
    )


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


def _resolve_hook_execution_root(root: Path, state: TaskState) -> Path:
    """Run runner hooks against the main workspace checkout.

    Hooks are expected to inspect and, when needed, mutate the main checkout
    so their side effects participate in the dirty-main cleanup path instead
    of being stranded inside a task worktree.
    """
    del state
    return root


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


def _mark_task_interrupted_on_crash(root: Path, task: TaskRecord, persistence: object) -> None:
    """Best-effort cleanup when run_task raises an unexpected exception.

    Clears active_task_id and marks the task as interrupted so the next
    runner start can resume it instead of finding stale "running" state.
    """
    try:
        from litehive.state.persist import load_state, save_state

        state = load_state(root)
        if state.active_task_id == task.id:
            state.active_task_id = None
            if task.id not in state.queue:
                state.queue.insert(0, task.id)
            save_state(root, state)
        fresh = get_task(root, task.id)
        if fresh is not None and fresh.runtime.execution_status == "running":
            fresh.runtime.execution_status = "interrupted"
            fresh.status = "queued"
            save_task(root, fresh)
    except Exception:
        pass  # best-effort — don't mask the original crash


def _cleanup_terminal_worktree(root: Path, task: TaskRecord | None) -> None:
    if task is None:
        return
    if task.status == "flagged" and task.flag_reason in _MANUAL_REVIEW_FLAG_REASONS:
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
    """Translate ``LitehiveConfig.runner_hooks`` into ``HookSpec`` lists."""
    out: dict[str, list[HookSpec]] = {}
    for phase, hooks in (getattr(config, "runner_hooks", None) or {}).items():
        specs = [
            HookSpec(
                command=str(spec_data["command"]),
                timeout_seconds=float(spec_data.get("timeout_seconds", 60)),
                description=None if spec_data.get("description") is None else str(spec_data["description"]),
                instructions_on_failure=(
                    None
                    if spec_data.get("instructions_on_failure") is None
                    else str(spec_data["instructions_on_failure"])
                ),
            )
            for hook in hooks or []
            for spec_data in [{"command": hook} if isinstance(hook, str) else hook]
        ]
        if specs:
            out[phase] = specs
    return out


def _report_stage_for_phase(phase: str) -> str:
    return _STAGE_TO_PIPELINE_STATUS.get(phase, phase)


def _record_hook_warnings(
    root: Path,
    task: TaskRecord,
    *,
    phase: str,
    warnings: list[str],
) -> None:
    report_stage = _report_stage_for_phase(phase)
    summary = f"Runner hooks at `{phase}` completed with warnings."
    feedback = "\n\n".join(warnings)
    report = StageReport(
        task_id=task.id,
        stage=report_stage,  # type: ignore[arg-type]
        verdict="pass",
        source="hook",
        summary=summary,
        feedback=cap_feedback(feedback),
        warnings=warnings,
        failure_diagnostics={
            "phase": phase,
            "source": "hook",
        },
    )
    report_path = record_stage_report(root, task, report)
    message = (
        f"{summary}\n\n"
        f"{feedback}\n\n"
        f"report: {report_path.relative_to(root)}"
    )
    append_task_activity(
        root,
        task,
        TaskActivityEntry(
            role="hook",
            stage=report_stage,
            verdict="comment",
            message=message,
        ),
    )
    append_journal(
        root,
        task,
        (
            f"Runner hooks at `{phase}` reported warnings.\n"
            f"report: `{report_path.relative_to(root)}`"
        ),
    )


def _record_hook_reject(
    root: Path,
    task: TaskRecord,
    *,
    phase: str,
    reason: str,
    warnings: list[str],
    hook: dict[str, str] | None,
    consecutive_same_hook_rejects: int | None,
) -> None:
    report_stage = _report_stage_for_phase(phase)
    summary = f"Runner hook at `{phase}` rejected the stage."
    feedback_parts = [reason, *warnings]
    feedback = "\n\n".join(part for part in feedback_parts if part)
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] = {
        "phase": phase,
        "source": "hook",
        "consecutive_same_hook_rejects": consecutive_same_hook_rejects,
    }
    if hook is not None:
        failure_diagnostics.update(
            {
                "point": hook.get("point"),
                "command": hook.get("command"),
                "description": hook.get("description"),
                "fingerprint": hook.get("fingerprint"),
            }
        )
    report = StageReport(
        task_id=task.id,
        stage=report_stage,  # type: ignore[arg-type]
        verdict="reject",
        source="hook",
        summary=summary,
        feedback=cap_feedback(feedback),
        warnings=warnings,
        failure_classification="hook_reject",
        failure_diagnostics=failure_diagnostics,
    )
    report_path = record_stage_report(root, task, report)
    message = (
        f"{summary}\n\n"
        f"{feedback}\n\n"
        f"report: {report_path.relative_to(root)}"
    )
    append_task_activity(
        root,
        task,
        TaskActivityEntry(
            role="hook",
            stage=report_stage,
            verdict="reject",
            message=message,
        ),
    )
    append_journal(
        root,
        task,
        (
            f"Runner hook at `{phase}` rejected the stage.\n"
            f"report: `{report_path.relative_to(root)}`"
        ),
    )


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
        persistence = SqlitePersistence(
            root,
            limits=replace(
                Limits(),
                rejection_loop_limit=resolve_task_rejection_loop_limit(task, config),
            ),
        )
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
        hook_runner = SubprocessHookRunner(
            root,
            execution_root_resolver=lambda state: _resolve_hook_execution_root(root, state),
        )
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
            transition_observer=lambda state, from_stage, event, trans: _observe_transition(
                root,
                state,
                from_stage,
                event,
                trans,
            ),
            session_store=sessions,
        )

        # 3. Run under the heartbeat so `litehive status` sees the active task.
        with runner_heartbeat(root, active_task_id=task.id):
            try:
                final_state = runner.run_task(task.id)
            except BaseException:
                # Runner crashed — mark task as interrupted so it can be
                # resumed instead of leaving stale "running" state behind.
                _mark_task_interrupted_on_crash(root, task, persistence)
                raise

        # 4. Mirror terminal state back to the v1 TaskRecord.
        updated_task = _sync_back(final_state, root) or task
        if final_state.stage in {"done", "failed"}:
            _clear_terminal_task_from_workspace_state(root, updated_task.id)
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


def _observe_transition(
    root: Path,
    state: TaskState,
    from_stage: str,
    event: object,
    trans: Transition,
) -> None:
    del trans
    task = get_task(root, state.task_id)
    if task is None:
        return
    if isinstance(event, HookOk) and event.warnings:
        _record_hook_warnings(
            root,
            task,
            phase=from_stage,
            warnings=event.warnings,
        )
        return
    if isinstance(event, Reject) and event.source == "hook":
        hook = event.metadata.get("hook")
        _record_hook_reject(
            root,
            task,
            phase=from_stage,
            reason=event.reason,
            warnings=[str(item) for item in event.metadata.get("warnings", [])],
            hook=hook if isinstance(hook, dict) else None,
            consecutive_same_hook_rejects=(
                event.metadata.get("consecutive_same_hook_rejects")
                if isinstance(event.metadata.get("consecutive_same_hook_rejects"), int)
                else None
            ),
        )
