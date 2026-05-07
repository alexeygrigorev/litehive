"""Shared internals for the task status transition modules.

The lifecycle transitions (requeue, resume, close, park, abandon, update)
all share the same audit/journal envelope and the same set of small
in-memory state mutations. Keeping them here lets each transition module
stay focused on its own decision logic without copy-pasting the
persistence and outcome-shaping plumbing.
"""

from litehive.domain.common import PipelineStatus, TaskExecutionStatus, TaskStatus
from litehive.domain.outcomes import OutcomeReasonCode, TaskCloseReason, TaskOutcomeKind
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.state.persist import persist_task_and_state_without_runner_guard_for_workspace
from litehive.tasks._process_signals import terminate_subagent_pid
from litehive.workspace import Workspace
from litehive.tasks.audit import (
    TaskAuditState,
    build_task_audit_entry,
)
from litehive.tasks.runtime import apply_task_outcome, clear_task_run_activity


# Process-signaling helper extracted to ``tasks/_process_signals.py``;
# re-aliased here so the existing ``_terminate_subagent_pid(...)`` call
# sites in the transition modules keep working without churn.
_terminate_subagent_pid = terminate_subagent_pid


def _reset_pipeline_state(workspace: Workspace, task_id: str, preserve_run_memory: bool = False) -> None:
    """
    Wipe the SQLite-side lifecycle/runtime rows before a new attempt.

    ``preserve_run_memory`` keeps the recovery-evidence trail so a requeue
    can feed prior failure context back to the next agent; without that
    flag the task starts truly clean (used by ``recover``-style operator
    actions).
    """
    SqlitePersistence(workspace).reset_current_lifecycle_state(
        task_id, preserve_run_memory=preserve_run_memory
    )


def _persist_transition(
    workspace: Workspace,
    task: TaskRecord,
    state: WorkspaceState,
    journal_message: str,
    action: str,
    actor: str,
    source: str,
    before_task: TaskRecord | TaskAuditState | None,
    before_queue: list[str],
    context: dict[str, object] | None = None,
) -> None:
    """
    Bundle a task/queue mutation with its audit entry in one transaction.

    Shared by every transition in this module so each status change emits
    one consistent journal+audit row; centralising the envelope keeps the
    audit shape uniform across requeue/resume/close/park/abandon and
    avoids each transition reinventing the build_task_audit_entry call.
    """

    persist_task_and_state_without_runner_guard_for_workspace(
        workspace,
        task=task,
        state=state,
        journal_message=journal_message,
        audit_entries=[
            build_task_audit_entry(
                task_id=task.id,
                action=action,
                actor=actor,
                source=source,
                before_task=before_task,
                after_task=task,
                before_queue=before_queue,
                after_queue=state.queue,
                context=context,
            )
        ],
    )


def _queue_task(state: WorkspaceState, task_id: str, front: bool = False) -> None:
    """
    Place a task into the workspace queue without creating a duplicate.

    Called by requeue and resume so a task that was already queued ends up
    at exactly one position; without the dedupe step the same task could
    be selected twice in a row and the dependent counts would skew.
    """
    state.queue = [item for item in state.queue if item != task_id]
    if front:
        state.queue.insert(0, task_id)
    else:
        state.queue.append(task_id)


def _apply_cancelled_task_state(task: TaskRecord, reason: str) -> None:
    """
    Project a task into the closed/cancelled shape used by ``abandon``.

    Clears any in-flight run, marks the task ``closed`` with
    ``execution_cancelled`` so downstream surfaces can distinguish a
    CLI-cancelled task from a user-closed one, and stamps a ``last_outcome``
    that future status views display.
    """
    clear_task_run_activity(task, execution_status=TaskExecutionStatus.CANCELLED)
    task.status = TaskStatus.CLOSED
    task.close_reason = "execution_cancelled"
    task.flag_reason = None
    apply_task_outcome(
        task,
        kind=TaskOutcomeKind.CLOSED,
        stage=task.pipeline_status,
        reason_code=OutcomeReasonCode.EXECUTION_CANCELLED,
        reason=reason,
        retry_count=0,
        retry_limit=0,
    )


def _apply_close_task_state(
    task: TaskRecord,
    close_reason: TaskCloseReason,
    reason: str | None,
    follow_up_task_id: str | None = None,
    pipeline_status: PipelineStatus | str | None = None,
) -> str:
    """
    Project a task into the done/closed shape and return the journal message.

    The ``litehive task close`` flow funnels every closure outcome
    (``done``/``wont_do``/``deferred``/``duplicate``) through here so
    status, pipeline_status, last_outcome, and journal text all come from
    one rule set; returning the journal text lets the caller persist it
    in the same audit envelope.
    """
    if close_reason == TaskCloseReason.DONE:
        execution_status = TaskExecutionStatus.DONE
    else:
        execution_status = TaskExecutionStatus.CANCELLED
    clear_task_run_activity(task, execution_status=execution_status)
    if close_reason == TaskCloseReason.DONE:
        task.status = TaskStatus.DONE
    else:
        task.status = TaskStatus.CLOSED
    task.close_reason = close_reason.value
    task.flag_reason = None
    if close_reason == TaskCloseReason.DONE:
        pipeline_status_fallback = PipelineStatus.DONE
    else:
        pipeline_status_fallback = task.pipeline_status
    resolved_pipeline_status = pipeline_status or pipeline_status_fallback
    task.pipeline_status = (
        resolved_pipeline_status
        if isinstance(resolved_pipeline_status, PipelineStatus)
        else PipelineStatus(resolved_pipeline_status)
    )
    outcome_kind = TaskOutcomeKind(task.status.value)
    apply_task_outcome(
        task,
        kind=outcome_kind,
        stage=task.pipeline_status,
        reason_code=close_reason.outcome_reason_code,
        reason=reason or close_reason.task_close_label,
        retry_count=0,
        retry_limit=0,
        follow_up_task_id=follow_up_task_id,
    )
    journal_message = f"Task closed: {close_reason.value}."
    if reason:
        journal_message += f" {reason}"
    if follow_up_task_id is not None:
        journal_message += f" Follow-up task: {follow_up_task_id}."
    return journal_message


def _apply_parked_task_state(task: TaskRecord) -> None:
    """
    Project a task into the parked shape used by ``park_task``.

    Clears the in-flight runtime so the runner will not pick the task up,
    then flips the task to ``parked`` so the operator can revisit it later
    via ``litehive task resume``.
    """
    clear_task_run_activity(task, execution_status=TaskExecutionStatus.PAUSED)
    task.status = TaskStatus.PARKED
