"""Shared internals for the task status transition modules.

The lifecycle transitions (requeue, resume, close, park, abandon, update)
all share the same audit/journal envelope and the same set of small
in-memory state mutations. Keeping them here lets each transition module
stay focused on its own decision logic without copy-pasting the
persistence and outcome-shaping plumbing.
"""

from pathlib import Path

from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.state.persist import persist_task_and_state_without_runner_guard
from litehive.tasks._process_signals import terminate_subagent_pid
from litehive.tasks.audit import (
    TaskAuditState,
    build_task_audit_entry,
)
from litehive.tasks.runtime import apply_task_outcome, clear_task_run_activity


# Process-signaling helper extracted to ``tasks/_process_signals.py``;
# re-aliased here so the existing ``_terminate_subagent_pid(...)`` call
# sites in the transition modules keep working without churn.
_terminate_subagent_pid = terminate_subagent_pid


_CLOSE_OUTCOME_REASON_CODES = {"done", "wont_do", "deferred", "duplicate", "execution_cancelled"}

_CLOSE_REASON_CODE_LABELS: dict[str, str] = {
    "done": "Task already satisfied.",
    "wont_do": "Task closed as won't do.",
    "deferred": "Task deferred.",
    "duplicate": "Task closed as duplicate.",
    "execution_cancelled": "Task abandoned via CLI.",
}


def _reset_pipeline_state(root: Path, task_id: str, preserve_run_memory: bool = False) -> None:
    """Wipe the SQLite-side lifecycle/runtime rows for a task before it starts a new attempt; preserve_run_memory keeps recovery evidence so a requeue still has the prior failure context to feed back."""
    SqlitePersistence(root).reset_current_lifecycle_state(task_id, preserve_run_memory=preserve_run_memory)


def _persist_transition(
    root: Path,
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
    """Bundle a task/queue mutation with its audit entry so every status transition emits one consistent journal+audit row; shared by every transition in this module to keep the audit shape uniform."""

    persist_task_and_state_without_runner_guard(
        root,
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
    """Place a task into the workspace queue without ever creating a duplicate entry; called by requeue and resume so a task that was already queued ends up at exactly one position."""
    state.queue = [item for item in state.queue if item != task_id]
    if front:
        state.queue.insert(0, task_id)
    else:
        state.queue.append(task_id)


def _apply_cancelled_task_state(task: TaskRecord, reason: str) -> None:
    clear_task_run_activity(task, execution_status="cancelled")
    task.status = TaskStatus.CLOSED
    task.close_reason = "execution_cancelled"
    task.flag_reason = None
    apply_task_outcome(
        task,
        kind="closed",
        stage=task.pipeline_status,
        reason_code="execution_cancelled",
        reason=reason,
        retry_count=0,
        retry_limit=0,
    )


def _apply_close_task_state(
    task: TaskRecord,
    outcome: str,
    reason: str | None,
    follow_up_task_id: str | None = None,
    pipeline_status: str | None = None,
) -> str:
    if outcome == "done":
        execution_status = "done"
    else:
        execution_status = "cancelled"
    clear_task_run_activity(task, execution_status=execution_status)
    if outcome == "done":
        task.status = TaskStatus.DONE
    else:
        task.status = TaskStatus.CLOSED
    task.close_reason = outcome
    task.flag_reason = None
    task.pipeline_status = pipeline_status or (PipelineStatus.DONE if outcome == "done" else task.pipeline_status)
    apply_task_outcome(
        task,
        kind=task.status,
        stage=task.pipeline_status,
        reason_code=outcome,
        reason=reason or _CLOSE_REASON_CODE_LABELS[outcome],
        retry_count=0,
        retry_limit=0,
        follow_up_task_id=follow_up_task_id,
    )
    journal_message = f"Task closed: {outcome}."
    if reason:
        journal_message += f" {reason}"
    if follow_up_task_id is not None:
        journal_message += f" Follow-up task: {follow_up_task_id}."
    return journal_message


def _apply_parked_task_state(task: TaskRecord) -> None:
    clear_task_run_activity(task, execution_status="paused")
    task.status = TaskStatus.PARKED
