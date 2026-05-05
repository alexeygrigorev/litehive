"""Task orchestration entry point.

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
6. Syncs the terminal state back to the ``TaskRecord`` so
   ``litehive status`` and the queue stay coherent.

Returns a small ``ExecutionResult`` named-tuple-ish dataclass the
caller can render.
"""

from dataclasses import dataclass, replace
from pathlib import Path

from litehive.config.loading import load_config
from litehive.config.engine_models import resolve_task_rejection_loop_limit, resolve_task_retry_policy
from litehive.git.ops import GitError, current_head
from litehive.domain.reports import (
    ReportPipelineState,
    StageReport,
    TaskActivityEntry,
    canonical_report_pipeline_state,
)
from litehive.domain.task import TaskRecord
from litehive.domain.runtime import RuntimeFailedRunRecord, RuntimeHookRejectFingerprint, RuntimeRecoveryOutcome
from litehive.domain.common import (
    PipelineState,
    PipelineStatus,
    TaskStage,
    TaskStatus,
    canonical_pipeline_state,
    cap_feedback,
    pipeline_status_for_pipeline_state,
    task_stage_for_pipeline_state,
    utcnow,
)
from litehive.state.records import (
    get_task,
    get_task_worktree_path,
    save_task,
    set_task_commit_sha,
)
from litehive.worktree import WorktreeService, cleanup_terminal_task_worktree, resolve_recorded_worktree_path
from litehive.tasks.activity import append_task_activity, latest_task_activity_entry
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.journal import append_journal
from litehive.tasks.report_storage import record_stage_report
from litehive.tasks.runtime import apply_task_outcome
from litehive.state.locking import persist_future_task_update
from litehive.state.locking import runner_heartbeat, workspace_runner_guard
from litehive.state.persist import load_state, persist_tasks_and_state, save_state

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
from .persistence import FailedRunRecord, Limits, SqlitePersistence, TaskNotFound, TaskState
from .registry import build_registry
from .runner import StateMachineRunner
from .sessions import SqliteSessionStore
from .transitions import Transition
from .types import PipelineMode


def _load_or_initialize(task_id: str, workspace_root: Path, persistence: SqlitePersistence) -> TaskState:
    """Return a ``TaskState`` for ``task_id``, creating the row if needed."""
    task_record = get_task(workspace_root, task_id)
    if task_record is None:
        raise LookupError(f"no task record for {task_id!r}")
    raw = task_record.pipeline_mode
    if isinstance(raw, str) and raw:
        mode = PipelineMode(raw)
    else:
        mode = PipelineMode.FULL
    entry_stage = _entry_stage_for_task(task_record)
    fresh_state_kwargs = dict(
        task_id=task_id,
        pipeline_mode=mode,
        stage=PipelineState.READY,
        entry_stage=entry_stage,
    )

    def _fresh_state(
        failed_run_history: dict[str, FailedRunRecord] | None = None,
        recovery_history: list | None = None,
        recovery_budget_history_start: int = 0,
    ) -> TaskState:
        state = TaskState(
            **fresh_state_kwargs,
            recovery_history=[] if recovery_history is None else recovery_history,
            recovery_budget_history_start=recovery_budget_history_start,
            failed_run_history={} if failed_run_history is None else failed_run_history,
            limits=persistence.limits,
        )
        persistence.save(state)
        return state

    if entry_stage is None:
        try:
            return persistence.load(task_id)
        except TaskNotFound:
            return _fresh_state()

    def _initialize_fresh_state() -> TaskState:
        preserved_failed_runs: dict[str, FailedRunRecord] = {}
        preserved_recovery_history = []
        recovery_budget_history_start = 0
        try:
            previous_state = persistence.load(task_id)
            preserved_failed_runs = previous_state.failed_run_history
            preserved_recovery_history = previous_state.recovery_history
            recovery_budget_history_start = len(previous_state.recovery_history)
        except TaskNotFound:
            preserved_failed_runs = {}
            preserved_recovery_history = []
            recovery_budget_history_start = 0
        persistence.reset_current_lifecycle_state(task_id, preserve_run_memory=True)
        return _fresh_state(
            failed_run_history=preserved_failed_runs,
            recovery_history=preserved_recovery_history,
            recovery_budget_history_start=recovery_budget_history_start,
        )

    try:
        state = persistence.load(task_id)
    except TaskNotFound:
        return _fresh_state()
    except Exception:
        raise

    if _stale_launch_state_requires_reset(task_record, state, pipeline_mode=mode, entry_stage=entry_stage):
        return _initialize_fresh_state()
    return state


def _entry_stage_for_task(task_record: TaskRecord) -> PipelineState | None:
    """Pick the pipeline stage a resumed task should re-enter on, or ``None`` if there's nothing to resume.

    Terminal/queue statuses (backlog, done, flagged) deliberately collapse to
    ``None`` so the caller starts a fresh pipeline rather than re-running a
    stage on a task that has already left the pipeline.
    """
    stage = (
        task_record.runtime.pipeline.current_stage.stage
        or (
            None
            if task_record.runtime.execution.interruption is None
            else task_record.runtime.execution.interruption.resume_stage
        )
        or task_record.pipeline_status
    )
    if stage in {None, PipelineStatus.BACKLOG, PipelineStatus.DONE, PipelineStatus.FLAGGED}:
        return None
    if stage == TaskStage.COMMIT_TO_GIT:
        return PipelineState.COMMIT
    return canonical_pipeline_state(stage)


def _launch_requires_fresh_pipeline_state(task_record: TaskRecord) -> bool:
    """Detect a queued resume of a previously-paused task so its sqlite state can be rebuilt before launch.

    Skips tasks already mid-flight (``running``); those keep their existing
    pipeline state.
    """
    return _entry_stage_for_task(task_record) is not None and task_record.runtime.pipeline.execution_status != "running"


def _stale_launch_state_requires_reset(
    task_record: TaskRecord,
    state: TaskState,
    pipeline_mode: PipelineMode,
    entry_stage: PipelineState,
) -> bool:
    """Decide whether a resumed task's persisted ``TaskState`` is incoherent with the launch request and must be reset.

    A row whose mode/entry_stage drifted from the resume request would
    otherwise re-enter the wrong stage; treat that as stale and rebuild.
    """
    if not _launch_requires_fresh_pipeline_state(task_record):
        return False
    if state.pipeline_mode != pipeline_mode:
        return True
    return state.stage != PipelineState.READY or state.entry_stage != entry_stage


_MANUAL_REVIEW_FLAG_REASONS = {
    "hook_reject_loop",
    "rejection_loop_detected",
    "time_budget_exceeded",
}


def _runtime_hook_reject_fingerprint(state: TaskState) -> RuntimeHookRejectFingerprint | None:
    """Project the lifecycle-side hook-reject fingerprint into the runtime-domain shape stored on TaskRecord."""
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
    """Flatten a lifecycle ``RecoveryOutcome`` (with a nested trigger) into the flat runtime record stored on TaskRecord."""
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
    """Build the dedup key used to collapse repeated recovery outcomes when projecting history into the runtime record."""
    return (
        outcome.origin_stage,
        outcome.fingerprint,
        outcome.budget_key,
        outcome.recovery_verdict,
        outcome.created_at,
    )


def _runtime_recovery_history_projection(current_state: TaskState) -> list[RuntimeRecoveryOutcome]:
    """Materialize a deduped recovery history for the TaskRecord so ``litehive status`` shows each outcome once."""
    projected: list[RuntimeRecoveryOutcome] = []
    seen: set[tuple[str | None, str, str, str, str | None]] = set()
    for item in [_runtime_recovery_outcome(outcome) for outcome in current_state.recovery_history]:
        key = _runtime_recovery_key(item)
        if key in seen:
            continue
        seen.add(key)
        projected.append(item)
    return projected


def _runtime_failed_run_record(record: FailedRunRecord) -> RuntimeFailedRunRecord:
    """Project a lifecycle ``FailedRunRecord`` to the runtime-domain shape exposed on TaskRecord for status views."""
    return RuntimeFailedRunRecord(
        stage=record.stage,
        failure_shape=record.failure_shape,
        count=record.count,
        first_at=record.first_at,
        latest_at=record.latest_at,
        last_reason=record.last_reason,
        source=record.source,
        classification=record.classification,
        retry_limit=record.retry_limit,
        failed_reason=record.failed_reason,
        operator_override_count=record.operator_override_count,
        last_operator_override_at=record.last_operator_override_at,
    )


def _runtime_failed_run_history_projection(current_state: TaskState) -> dict[str, RuntimeFailedRunRecord]:
    """Stringify the failed-run history keys so the runtime record stays JSON-friendly for status snapshots."""
    return {str(key): _runtime_failed_run_record(record) for key, record in current_state.failed_run_history.items()}


def _sync_runtime_fields(task_record: TaskRecord, state: TaskState) -> None:
    """Mirror the runner's pipeline state onto ``task_record.runtime`` so observability surfaces (status, queue) stay coherent.

    Terminal stages (DONE/FAILED) clear ``current_stage`` and freeze
    ``execution_status``; non-terminal stages refresh ``started_at`` only on
    actual stage transitions.
    """
    now = utcnow()
    task_record.runtime.pipeline.consecutive_same_hook_rejects = state.consecutive_same_hook_rejects
    task_record.runtime.pipeline.last_hook_reject_fingerprint = _runtime_hook_reject_fingerprint(state)
    task_record.runtime.pipeline.hook_reject_recovery_invoked = state.hook_reject_recovery_invoked
    task_record.runtime.pipeline.recovery_history = _runtime_recovery_history_projection(state)
    task_record.runtime.pipeline.failed_run_history = _runtime_failed_run_history_projection(state)
    if state.stage in {PipelineState.DONE, PipelineState.FAILED}:
        # Set appropriate terminal execution status
        if state.stage == PipelineState.DONE:
            task_record.runtime.pipeline.execution_status = "done"
        else:  # PipelineState.FAILED
            task_record.runtime.pipeline.execution_status = "failed"
        task_record.runtime.pipeline.current_stage = task_record.runtime.pipeline.current_stage.model_copy(
            update={
                "stage": None,
                "status": "idle",
                "started_at": None,
                "updated_at": now,
            }
        )
        return
    current_stage = task_record.runtime.pipeline.current_stage
    if current_stage.stage == state.stage:
        started_at = current_stage.started_at
    else:
        started_at = now
    task_record.runtime.pipeline.execution_status = "running"
    task_record.runtime.pipeline.current_stage = current_stage.model_copy(
        update={
            "stage": state.stage,
            "status": "running",
            "started_at": started_at,
            "updated_at": now,
        }
    )


def _latest_recovery_trigger(state: TaskState):
    """Return whichever recovery trigger best describes ``state``: the active one, or the most recent historical one."""
    if state.active_recovery_trigger is not None:
        return state.active_recovery_trigger
    if state.recovery_history:
        return state.recovery_history[-1].trigger
    return None


def _recovery_origin_stage(origin_stage: str | None) -> str | None:
    """Translate a pipeline-state origin into its task-stage label for operator-facing surfaces, leaving unknown values intact."""
    if origin_stage is None:
        return None
    try:
        task_stage = task_stage_for_pipeline_state(origin_stage)
    except ValueError:
        return origin_stage
    if task_stage is None:
        return origin_stage
    return str(task_stage)


def _sync_terminal_status(task_record: TaskRecord, state: TaskState) -> str | None:
    """Translate a terminal pipeline outcome into the ``TaskStatus``/``flag_reason`` the queue and CLI rely on.

    Splits FAILED across flag-reason categories (hook-reject loop, semantic
    reject, time budget, recovery exhaustion, merge failure, crash budget) so
    operator-facing tools can route follow-up actions correctly. Returns an
    optional journal note about reconciled commits.
    """
    journal_message: str | None = None
    commit_result = state.commit_result
    if state.stage == PipelineState.DONE:
        task_record.status = TaskStatus.DONE
        task_record.pipeline_status = PipelineStatus.DONE
        task_record.close_reason = "done"
        task_record.flag_reason = None
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
    elif state.stage == PipelineState.FAILED:
        trigger = _latest_recovery_trigger(state)
        if trigger is not None:
            origin_stage = trigger.origin_stage
        else:
            origin_stage = None
        if hasattr(state.failed_reason, "value"):
            failed_reason = state.failed_reason.value
        else:
            failed_reason = state.failed_reason
        merge_reject = state.last_rejection_by_stage.get(PipelineState.MERGE_RESOLVING)
        if origin_stage == PipelineState.MERGE_RESOLVING or merge_reject is not None:
            task_record.status = TaskStatus.FLAGGED
            task_record.pipeline_status = PipelineStatus.FLAGGED
            task_record.close_reason = None
            task_record.flag_reason = "merge_failed"
            if state.failed_message:
                journal_message = f"commit_to_git failed during merge reconciliation: {state.failed_message}"
        else:
            task_record.status = TaskStatus.FLAGGED
            task_record.pipeline_status = PipelineStatus.FLAGGED
            task_record.close_reason = None
            if failed_reason == "hook_reject_loop" or (
                trigger is not None and trigger.reason_code == "hook_reject_loop"
            ):
                task_record.flag_reason = "hook_reject_loop"
            elif failed_reason == "rejection_loop_detected":
                task_record.flag_reason = "rejection_loop_detected"
            elif failed_reason == "semantic_reject":
                task_record.flag_reason = "semantic_reject"
            elif failed_reason == "time_budget_exceeded":
                task_record.flag_reason = "time_budget_exceeded"
            elif failed_reason == "recovery_exhausted":
                task_record.flag_reason = "recovery_failed"
            elif failed_reason == "recovery_budget_hit":
                if trigger is not None:
                    trigger_kind = trigger.trigger_event_kind.value
                else:
                    trigger_kind = None
                if trigger_kind in {"crash", "timeout"}:
                    task_record.flag_reason = "crash_budget_exhausted"
                else:
                    task_record.flag_reason = "recovery_budget_exhausted"
    else:
        task_record.status = TaskStatus.IN_PROGRESS
        task_record.close_reason = None
        task_record.flag_reason = None
        task_record.pipeline_status = pipeline_status_for_pipeline_state(state.stage)
    return journal_message


def _sync_back(state: TaskState, workspace_root: Path) -> TaskRecord | None:
    """Mirror the pipeline stage back to the TaskRecord so litehive status stays accurate."""
    task_record = get_task(workspace_root, state.task_id)
    if task_record is None:
        return None
    before_task = snapshot_task_audit_state(task_record)
    before_last_outcome = task_record.runtime.pipeline.last_outcome.model_copy(deep=True)
    _sync_runtime_fields(task_record, state)
    journal_message = _sync_terminal_status(task_record, state)
    _sync_recovery_follow_up(workspace_root, task_record, state)
    audit_entries = []
    if (
        before_task.status != task_record.status
        or before_task.pipeline_status != task_record.pipeline_status
        or before_last_outcome != task_record.runtime.pipeline.last_outcome
    ):
        action = "status_changed"
        if state.stage == PipelineState.FAILED:
            action = "failed"
        elif state.stage == PipelineState.DONE and task_record.status == TaskStatus.DONE:
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
                        None
                        if state.failed_reason is None
                        else getattr(state.failed_reason, "value", state.failed_reason)
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
    """When recovery exhausts and escalates to a follow-up task, mirror that escalation onto the original task's last_outcome.

    Only fires for the recovery-exhausted exit path; other failure shapes are
    handled by ``_sync_terminal_status``.
    """
    if hasattr(state.failed_reason, "value"):
        failed_reason = state.failed_reason.value
    else:
        failed_reason = state.failed_reason
    if state.stage != PipelineState.FAILED:
        return
    if failed_reason != "recovery_exhausted":
        return
    latest = latest_task_activity_entry(
        root,
        task_record,
        role="recovery",
        stage=PipelineState.RECOVERING.value,
        verdicts={"reject"},
    )
    if latest is None or not latest.follow_up_task_id:
        return
    trigger = _latest_recovery_trigger(state)
    apply_task_outcome(
        task_record,
        kind="flagged",
        stage=(trigger.origin_stage if trigger is not None else PipelineState.RECOVERING.value),
        reason_code="stage_exception",
        reason=state.failed_message or latest.message or "Recovery escalated to a follow-up task.",
        retry_count=task_record.runtime.pipeline.retry_count,
        retry_limit=task_record.runtime.pipeline.retry_limit,
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
    """Drop a finished task from the active slot and queue so the next runner tick selects a different task."""
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
    """Run pre-commit hooks in the task checkout and keep after_commit on main."""
    if state.stage == PipelineState.AFTER_COMMIT:
        return root
    return _resolve_worktree(root, state)


def _task_recorded_worktree(root: Path, task_id: str) -> tuple[TaskRecord | None, Path | None]:
    """Look up a task and its on-disk worktree path together so commit/sync nodes can resolve both in one go without a second db hit."""
    task = get_task(root, task_id)
    if task is None:
        return None, None
    recorded = get_task_worktree_path(task)
    if not recorded:
        return task, None
    return task, resolve_recorded_worktree_path(root, recorded)


def build_commit_node(root: Path) -> CommitNode:
    """Return the production ``GitCommitNode`` bound to this workspace."""
    return GitCommitNode(
        root,
        worktree_resolver=lambda state: _resolve_worktree(root, state),
        task_resolver=lambda state: _task_recorded_worktree(root, state.task_id)[0],
    )


def _build_worktree_sync_node(root: Path) -> GitWorktreeSyncNode:
    """Return the production ``GitWorktreeSyncNode`` bound to this workspace."""
    return GitWorktreeSyncNode(
        workspace_root=root,
        worktree_resolver=lambda state: _resolve_worktree(root, state),
    )


def _worktree_missing_probe(root: Path):
    """Return a probe callable backed by the worktree service."""
    service = WorktreeService(root)

    def _probe(state) -> bool:
        return service.task_has_missing_recorded_worktree(state.task_id)

    return _probe


def _worktree_metadata_repair(root: Path):
    """Return a stale worktree metadata repair backed by the worktree service."""
    service = WorktreeService(root)

    def _repair(state) -> None:
        service.clear_missing_recorded_worktree(state.task_id)

    return _repair


def _mark_task_interrupted_on_crash(root: Path, task: TaskRecord, persistence: object) -> None:
    """Best-effort cleanup when run_task raises an unexpected exception.

    Clears active_task_id and marks the task as interrupted so the next
    runner start can resume it instead of finding stale "running" state.
    """
    try:
        state = load_state(root)
        if state.active_task_id == task.id:
            state.active_task_id = None
            if task.id not in state.queue:
                state.queue.insert(0, task.id)
            save_state(root, state)
        fresh = get_task(root, task.id)
        if fresh is not None and fresh.runtime.pipeline.execution_status == "running":
            fresh.runtime.pipeline.execution_status = "interrupted"
            fresh.status = TaskStatus.QUEUED
            save_task(root, fresh)
    except Exception:
        pass  # best-effort — don't mask the original crash


def _cleanup_terminal_worktree(root: Path, task: TaskRecord | None) -> None:
    """Tear down a finished task's worktree, but preserve worktrees for tasks flagged for manual review so an operator can inspect them."""
    if task is None:
        return
    fresh = get_task(root, task.id)
    if fresh is not None:
        task = fresh
    if task.status == TaskStatus.FLAGGED and task.flag_reason in _MANUAL_REVIEW_FLAG_REASONS:
        return
    cleanup_terminal_task_worktree(root, task)


def reconcile_terminal_commit_sha(
    root: Path,
    task: TaskRecord | None,
    final_state: TaskState,
    persistence: SqlitePersistence,
) -> TaskRecord | None:
    """Backfill the integration commit SHA on a DONE task when the runner path didn't already record one.

    Falls back to reloading the commit_result from sqlite, then to ``git
    rev-parse HEAD``, so terminal status views never display a DONE task with
    an empty commit_sha.
    """
    if task is None or final_state.stage != PipelineState.DONE:
        return task
    if task.git.commit_sha and task.runtime.pipeline.git.commit_sha:
        return task

    commit_result = final_state.commit_result
    if commit_result is None:
        try:
            commit_result = persistence.load(final_state.task_id).commit_result
        except TaskNotFound:
            commit_result = None
    if commit_result is None:
        return task

    head_sha = commit_result.head_sha or current_head(root)
    if not head_sha:
        return task

    set_task_commit_sha(task, head_sha)
    save_task(root, task)
    return task


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


def _report_stage_for_phase(phase: str | PipelineState) -> ReportPipelineState:
    """Project a pipeline phase to the typed :class:`ReportPipelineState`.

    Hook-emitted reports use whichever stage the runner was passing
    through when the hook fired. We collapse internal node states down
    to their owning task stage, except merge-resolving and recovering,
    which are first-class report stages.
    """
    state = canonical_pipeline_state(phase)
    if state == PipelineState.MERGE_RESOLVING:
        return PipelineState.MERGE_RESOLVING.value
    if state == PipelineState.RECOVERING:
        return PipelineState.RECOVERING.value
    task_stage = task_stage_for_pipeline_state(state)
    if task_stage is None:
        # No task-stage projection exists; fall back to the report
        # converter, which raises if the value is also not a valid
        # report stage.
        return canonical_report_pipeline_state(str(state))
    return task_stage


def _record_hook_warnings(
    root: Path,
    task: TaskRecord,
    phase: str,
    warnings: list[str],
) -> None:
    report_stage = _report_stage_for_phase(phase)
    summary = f"Runner hooks at `{phase}` completed with warnings."
    feedback = "\n\n".join(warnings)
    report = StageReport(
        task_id=task.id,
        pipeline_state=report_stage,
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
    message = f"{summary}\n\n{feedback}\n\nreport: {report_path.relative_to(root)}"
    append_task_activity(
        root,
        task,
        TaskActivityEntry(
            role="hook",
            stage=str(report_stage),
            verdict="comment",
            message=message,
        ),
    )
    append_journal(
        root,
        task,
        (f"Runner hooks at `{phase}` reported warnings.\nreport: `{report_path.relative_to(root)}`"),
    )


def _record_hook_reject(
    root: Path,
    task: TaskRecord,
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
        pipeline_state=report_stage,
        verdict="reject",
        source="hook",
        summary=summary,
        feedback=cap_feedback(feedback),
        warnings=warnings,
        failure_classification="hook_reject",
        failure_diagnostics=failure_diagnostics,
    )
    report_path = record_stage_report(root, task, report)
    message = f"{summary}\n\n{feedback}\n\nreport: {report_path.relative_to(root)}"
    append_task_activity(
        root,
        task,
        TaskActivityEntry(
            role="hook",
            stage=str(report_stage),
            verdict="reject",
            message=message,
        ),
    )
    append_journal(
        root,
        task,
        (f"Runner hook at `{phase}` rejected the stage.\nreport: `{report_path.relative_to(root)}`"),
    )


def run_task(
    root: Path,
    task: TaskRecord,
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
            check_quota=engine_factory is None,
        )
        sessions = SqliteSessionStore(root)
        journal = SqliteJournal(root)
        hook_runner = SubprocessHookRunner(
            root,
            execution_root_resolver=lambda state: _resolve_hook_execution_root(root, state),
        )
        commit_node = build_commit_node(root)
        worktree_sync_node = _build_worktree_sync_node(root)
        ready_node = ReadyNode(probes=[_worktree_missing_probe(root)])
        pre_exec_recovery_node = PreExecRecoveryNode(
            repairs=[_worktree_metadata_repair(root)],
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
            task_time_budget_seconds=config.task_time_budget_seconds,
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

        # 4. Mirror terminal state back to the TaskRecord.
        updated_task = _sync_back(final_state, root) or task
        if final_state.stage in {PipelineState.DONE, PipelineState.FAILED}:
            updated_task = reconcile_terminal_commit_sha(
                root,
                updated_task,
                final_state=final_state,
                persistence=persistence,
            )
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
    """State-machine transition hook that turns hook outcomes into stage reports and journal/activity entries.

    Only hook events produce reports here; engine events are reported by the
    nodes themselves. The runner invokes this on every transition.
    """
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
