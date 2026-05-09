"""Task status transitions for taking a task out of the active queue.

Covers close (terminal verdict: done/wont_do/deferred/duplicate/
execution_cancelled), abandon (operator-initiated kill of an in-flight or
parked task), and park (set aside without closing so the operator can
resume later).
"""

from litehive.domain.common import TaskStatus
from litehive.domain.outcomes import TaskCloseReason
from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import StopTaskSummary
from litehive.workspace import Workspace

from litehive.tasks.constants import (
    CLOSED_TASK_STATUSES,
    RESUMABLE_TASK_STATUSES,
)
from litehive.state.locking import (
    WorkspaceMutationGuard,
    WorkspaceRunnerLock,
    WorkspaceStateLock,
)
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import WorkspaceTasks
from litehive.tasks.audit import snapshot_task_audit_state
from litehive.tasks.queue import TaskQueueService
from litehive.tasks.stop import _stop_current_task
from litehive.tasks._status_helpers import (
    _apply_cancelled_task_state,
    _apply_close_task_state,
    _apply_parked_task_state,
    _persist_transition,
    _reset_pipeline_state,
    _terminate_subagent_pid,
)


def _allowed_close_outcome_values() -> list[str]:
    """
    Return the CLI-supported close outcome spellings.

    TaskCloseReason carries only the terminal operator choices accepted
    by the close transition.
    """
    return sorted(reason.value for reason in TaskCloseReason)


def _abandon_task_transition(
    workspace: Workspace,
    task_id: str,
    reason: str = "Task abandoned via CLI.",
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """
    Cancel an in-flight or parked task.

    Signals the live subagent (if any), marks the task closed/cancelled,
    and drops it from the queue. Differs from ``close_task`` in being the
    operator-initiated kill path: ``close`` records a deliberate terminal
    outcome, ``abandon`` says "stop right now and tear it down".
    """
    with WorkspaceStateLock(workspace).hold():
        task = WorkspaceTasks(workspace).require(task_id)
        before_task = snapshot_task_audit_state(task)
        state = WorkspaceStateRepository(workspace).load()
        queue_before = list(state.queue)
        WorkspaceMutationGuard(workspace).ensure_future_task_mutation_allowed([task.id], state=state)
        if task.status not in {TaskStatus.FLAGGED, *CLOSED_TASK_STATUSES, *RESUMABLE_TASK_STATUSES}:
            raise ValueError(f"Task {task.id} is not interrupted, parked, flagged, or closed")
        if task.runtime.execution.active_subagent is None:
            active_subagent_pid = None
        else:
            active_subagent_pid = task.runtime.execution.active_subagent.pid
        _terminate_subagent_pid(
            task.id,
            active_subagent_pid,
        )
        _apply_cancelled_task_state(task, reason=reason)
        TaskQueueService.remove_from_state(state, task.id)
        _persist_transition(
            workspace,
            task=task,
            state=state,
            journal_message=f"{reason.rstrip('.')} at stage `{task.pipeline_status}`.",
            action="abandoned",
            actor=audit_actor,
            source=audit_source,
            before_task=before_task,
            before_queue=queue_before,
            context={"reason": reason},
        )
        _reset_pipeline_state(workspace, task.id)
        return task


def _close_task_transition(
    workspace: Workspace,
    task_id: str,
    outcome: str,
    reason: str | None = None,
    follow_up_task_id: str | None = None,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """
    Mark a task as explicitly closed with a terminal outcome.

    Valid outcomes are ``done``, ``wont_do``, ``deferred``, ``duplicate``,
    and ``execution_cancelled``. The task is removed from the queue and any
    live runner/subagent is signalled so a closed task does not keep
    consuming runtime resources.
    """
    try:
        close_reason = TaskCloseReason(outcome)
    except ValueError:
        allowed = ", ".join(_allowed_close_outcome_values())
        raise ValueError(f"Unsupported close outcome '{outcome}'. Expected one of: {allowed}")
    state = WorkspaceStateRepository(workspace).load()
    stop_summary: StopTaskSummary | None = None
    task_snapshot = WorkspaceTasks(workspace).get_record(task_id)
    if task_snapshot is None or task_snapshot.runtime.execution.active_subagent is None:
        active_subagent_pid = None
    else:
        active_subagent_pid = task_snapshot.runtime.execution.active_subagent.pid
    runner_lock = WorkspaceRunnerLock(workspace)
    if runner_lock.is_held():
        runner_metadata = runner_lock.read_metadata()
    else:
        runner_metadata = None
    if state.active_task_id == task_id or (runner_metadata is not None and runner_metadata.active_task_id == task_id):
        stop_summary = _stop_current_task(workspace)
    if stop_summary is None:
        runner_pid = None
    else:
        runner_pid = stop_summary.runner_pid
    _terminate_subagent_pid(task_id, active_subagent_pid)
    _terminate_subagent_pid(task_id, runner_pid)
    with WorkspaceStateLock(workspace).hold():
        tasks = WorkspaceTasks(workspace)
        task = tasks.get_record(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        before_task = snapshot_task_audit_state(task)
        if follow_up_task_id is not None:
            follow_up_task_id = follow_up_task_id.strip()
            if not follow_up_task_id:
                raise ValueError("Follow-up task id must not be empty")
            if follow_up_task_id == task.id:
                raise ValueError(f"Task {task.id} cannot reference itself as a follow-up task")
            if tasks.get_record(follow_up_task_id) is None:
                raise ValueError(f"Task {follow_up_task_id} not found")
        state = WorkspaceStateRepository(workspace).load()
        queue_before = list(state.queue)
        WorkspaceMutationGuard(workspace).ensure_future_task_mutation_allowed([task.id], state=state)
        if task.status == TaskStatus.DONE:
            raise ValueError(f"Task {task.id} is already done and cannot be closed")
        journal_message = _apply_close_task_state(
            task,
            close_reason=close_reason,
            reason=reason,
            follow_up_task_id=follow_up_task_id,
        )
        TaskQueueService.remove_from_state(state, task.id)
        _persist_transition(
            workspace,
            task=task,
            state=state,
            journal_message=journal_message,
            action="closed",
            actor=audit_actor,
            source=audit_source,
            before_task=before_task,
            before_queue=queue_before,
            context={
                "outcome": close_reason.value,
                "reason": reason,
                "follow_up_task_id": follow_up_task_id,
            },
        )
        _reset_pipeline_state(workspace, task.id)
        return task


def _park_task_transition(
    workspace: Workspace,
    task_id: str,
    reason: str = "Task parked via CLI.",
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """
    Mark a task as parked and remove it from the queue.

    Parked tasks are still visible in status surfaces but no longer
    selectable by the runner; the operator brings them back via
    ``litehive task resume`` when ready.
    """
    with WorkspaceStateLock(workspace).hold():
        task = WorkspaceTasks(workspace).require(task_id)
        before_task = snapshot_task_audit_state(task)
        state = WorkspaceStateRepository(workspace).load()
        queue_before = list(state.queue)
        WorkspaceMutationGuard(workspace).ensure_future_task_mutation_allowed([task.id], state=state)
        if task.status == TaskStatus.DONE:
            raise ValueError(f"Task {task.id} is already done and cannot be parked")
        _apply_parked_task_state(task)
        TaskQueueService.remove_from_state(state, task.id)
        _persist_transition(
            workspace,
            task=task,
            state=state,
            journal_message=f"{reason.rstrip('.')} at stage `{task.pipeline_status}`.",
            action="parked",
            actor=audit_actor,
            source=audit_source,
            before_task=before_task,
            before_queue=queue_before,
            context={"reason": reason},
        )
        return task
