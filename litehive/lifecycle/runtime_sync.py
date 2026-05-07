"""Project lifecycle TaskState back onto the runtime TaskRecord.

These helpers translate the runner's internal pipeline state into the
runtime/observability shape (``task.runtime.pipeline.*`` fields,
``status``/``flag_reason``, audit entries, escalation follow-ups) so
``litehive status`` and the queue stay coherent after each transition.
"""

from litehive.domain.common import (
    OutcomeKind,
    OutcomeReasonCode,
    PipelineState,
    PipelineStatus,
    RuntimeStageStatus,
    TaskExecutionStatus,
    TaskStatus,
    pipeline_status_for_pipeline_state,
    task_stage_for_pipeline_state,
    utcnow,
)
from litehive.domain.runtime import (
    RuntimeFailedRunRecord,
    RuntimeHookRejectFingerprint,
    RuntimeRecoveryOutcome,
)
from litehive.domain.task import TaskRecord
from litehive.state.locking import persist_future_task_update_for_workspace
from litehive.state.persist import load_state_for_workspace, persist_tasks_and_state_for_workspace
from litehive.state.records import set_task_commit_sha
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.runtime import apply_task_outcome
from litehive.workspace import Workspace

from .persistence import FailedRunRecord, TaskState


_MANUAL_REVIEW_FLAG_REASONS = {
    "hook_reject_loop",
    "rejection_loop_detected",
    "time_budget_exceeded",
}


def _runtime_hook_reject_fingerprint(state: TaskState) -> RuntimeHookRejectFingerprint | None:
    """
    Project the lifecycle hook-reject fingerprint to the runtime shape.

    Lifecycle and runtime use distinct dataclasses because the runtime
    side is part of the public TaskRecord exposed to status/CLI views,
    so the lifecycle struct must be re-serialised into the runtime
    type rather than passed through as-is.
    """
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
    """
    Flatten a lifecycle ``RecoveryOutcome`` (with a nested trigger)
    into the flat runtime record stored on TaskRecord.

    The runtime shape is a single struct with no nesting because that
    is what the operator-facing JSON snapshot expects; the lifecycle
    shape keeps the trigger nested so it can carry the fingerprint
    payload into the recovery-budget logic without rebuilding it.
    """
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
    """
    Identity tuple used to dedupe recovery outcomes during projection.

    Two outcomes that match on origin/fingerprint/budget/verdict and
    timestamp describe the same recovery attempt persisted twice; the
    projector below collapses them so ``litehive status`` shows each
    attempt only once.
    """
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
    """
    Project a lifecycle ``FailedRunRecord`` to its runtime twin.

    Same field set, separate struct because the runtime side is part
    of the operator-facing TaskRecord shape and must stay decoupled
    from the lifecycle storage struct so a lifecycle field rename
    cannot silently change a status payload.
    """
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
            task_record.runtime.pipeline.execution_status = TaskExecutionStatus.DONE
        else:  # PipelineState.FAILED
            task_record.runtime.pipeline.execution_status = TaskExecutionStatus.FAILED
        task_record.runtime.pipeline.current_stage = task_record.runtime.pipeline.current_stage.model_copy(
            update={
                "stage": None,
                "status": RuntimeStageStatus.IDLE,
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
    task_record.runtime.pipeline.execution_status = TaskExecutionStatus.RUNNING
    task_record.runtime.pipeline.current_stage = current_stage.model_copy(
        update={
            "stage": state.stage,
            "status": RuntimeStageStatus.RUNNING,
            "started_at": started_at,
            "updated_at": now,
        }
    )


def _latest_recovery_trigger(state: TaskState):
    """
    Return whichever recovery trigger best describes the task right now.

    Prefers the live ``active_recovery_trigger`` because that reflects
    the in-flight failure; falls back to the most recent historical
    trigger so a terminal ``_sync_terminal_status`` call still has a
    trigger to attribute the failure to after the active one was
    cleared.
    """
    if state.active_recovery_trigger is not None:
        return state.active_recovery_trigger
    if state.recovery_history:
        return state.recovery_history[-1].trigger
    return None


def _recovery_origin_stage(origin_stage: str | None) -> str | None:
    """
    Translate a pipeline-state origin label into its task-stage label.

    Operator-facing surfaces report stages in task-stage terms, but
    the lifecycle stores them as pipeline states; unknown values pass
    through so a future stage that has no task-stage projection
    surfaces as itself rather than disappearing.
    """
    if origin_stage is None:
        return None
    try:
        task_stage = task_stage_for_pipeline_state(origin_stage)
    except ValueError:
        return origin_stage
    if task_stage is None:
        return origin_stage
    return task_stage.value


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


def _sync_back(state: TaskState, workspace: Workspace) -> TaskRecord | None:
    """
    Mirror the pipeline stage back to the TaskRecord.

    Called after every state-machine step so ``litehive status`` and
    the queue stay coherent with the runner's view; without this,
    operator-facing surfaces would drift from the actual lifecycle
    state until the task ends.
    """
    task_record = workspace.get_task(state.task_id)
    if task_record is None:
        return None
    before_task = snapshot_task_audit_state(task_record)
    before_last_outcome = task_record.runtime.pipeline.last_outcome.model_copy(deep=True)
    _sync_runtime_fields(task_record, state)
    journal_message = _sync_terminal_status(task_record, state)
    _sync_recovery_follow_up(workspace, task_record, state)
    audit_entries = []
    if before_task is not None and (
        before_task.status != task_record.status
        or before_task.pipeline_status != task_record.pipeline_status
        or before_last_outcome != task_record.runtime.pipeline.last_outcome
    ):
        action = "status_changed"
        if state.stage == PipelineState.FAILED:
            action = "failed"
        elif state.stage == PipelineState.DONE and task_record.status == TaskStatus.DONE:
            action = "completed"
        if state.failed_reason is None:
            failed_reason_value = None
        else:
            failed_reason_value = getattr(state.failed_reason, "value", state.failed_reason)
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
                    "failed_reason": failed_reason_value,
                    "failed_message": state.failed_message,
                },
            )
        )
    persist_future_task_update_for_workspace(
        workspace,
        task_record,
        journal_message=journal_message,
        audit_entries=audit_entries or None,
    )
    return task_record


def _sync_recovery_follow_up(workspace: Workspace, task_record: TaskRecord, state: TaskState) -> None:
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
    latest = workspace.task_activity(task_record).latest_entry(
        role="recovery",
        stage=PipelineState.RECOVERING,
        verdicts={"reject"},
    )
    if latest is None or not latest.follow_up_task_id:
        return
    trigger = _latest_recovery_trigger(state)
    if trigger is not None:
        trigger_stage = trigger.origin_stage or PipelineState.RECOVERING.value
        failure_classification = trigger.failure_fingerprint.budget_key()
        diagnostics_origin_stage = trigger.origin_stage
        diagnostics_trigger_event_kind = trigger.trigger_event_kind.value
        diagnostics_fingerprint = trigger.failure_fingerprint.fingerprint
        diagnostics_budget_key = trigger.budget_key()
    else:
        trigger_stage = PipelineState.RECOVERING.value
        failure_classification = None
        diagnostics_origin_stage = None
        diagnostics_trigger_event_kind = None
        diagnostics_fingerprint = None
        diagnostics_budget_key = None
    apply_task_outcome(
        task_record,
        kind=OutcomeKind.FLAGGED,
        stage=trigger_stage,
        reason_code=OutcomeReasonCode.STAGE_EXCEPTION,
        reason=state.failed_message or latest.message or "Recovery escalated to a follow-up task.",
        retry_count=task_record.runtime.pipeline.retry_count,
        retry_limit=task_record.runtime.pipeline.retry_limit,
        follow_up_task_id=latest.follow_up_task_id,
        failure_classification=failure_classification,
        failure_diagnostics={
            "origin_stage": diagnostics_origin_stage,
            "trigger_event_kind": diagnostics_trigger_event_kind,
            "fingerprint": diagnostics_fingerprint,
            "budget_key": diagnostics_budget_key,
        },
    )


def _clear_terminal_task_from_workspace_state(workspace: Workspace, task_id: str) -> None:
    """
    Drop a finished task from the active slot and queue.

    Without this, the next runner tick would re-select the same
    finished task and either re-run it (if it can find work to do) or
    spin in a tight no-op loop until an operator manually cleared the
    queue.
    """
    state = load_state_for_workspace(workspace)
    if state.active_task_id == task_id:
        state.active_task_id = None
    if task_id in state.queue:
        state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
    persist_tasks_and_state_for_workspace(
        workspace,
        tasks=(),
        state=state,
        protected_task_ids=[task_id],
    )
