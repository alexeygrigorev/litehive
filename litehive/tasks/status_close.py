"""Task status transitions for taking a task out of the active queue.

Covers ``close_task`` (terminal verdict: done/wont_do/deferred/duplicate/
execution_cancelled), ``abandon_task`` (operator-initiated kill of an
in-flight or parked task), and ``park_task`` (set aside without closing
so the operator can resume later).
"""

from pathlib import Path

from litehive.domain.common import TaskStatus
from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import StopTaskSummary

from litehive.tasks.constants import (
    CLOSED_TASK_STATUSES,
    RESUMABLE_TASK_STATUSES,
)
from litehive.state.locking import (
    ensure_future_task_mutation_allowed,
    read_runner_lock_metadata,
    runner_lock_is_held,
    workspace_lock,
)
from litehive.state.persist import load_state
from litehive.state.records import get_task_record, require_task
from litehive.tasks.audit import snapshot_task_audit_state
from litehive.tasks.queue import drop_task_from_workspace_state
from litehive.tasks.stop import stop_current_task
from litehive.tasks._status_helpers import (
    _CLOSE_OUTCOME_REASON_CODES,
    _apply_cancelled_task_state,
    _apply_close_task_state,
    _apply_parked_task_state,
    _persist_transition,
    _reset_pipeline_state,
    _terminate_subagent_pid,
)


def _abandon_task_transition(
    root: Path,
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

    with workspace_lock(root):
        task = require_task(root, task_id)
        before_task = snapshot_task_audit_state(task)
        state = load_state(root)
        queue_before = list(state.queue)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
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
        drop_task_from_workspace_state(state, task.id)
        _persist_transition(
            root,
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
        _reset_pipeline_state(root, task.id)
        return task


def _close_task_transition(
    root: Path,
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
    if outcome not in _CLOSE_OUTCOME_REASON_CODES:
        allowed = ", ".join(sorted(_CLOSE_OUTCOME_REASON_CODES))
        raise ValueError(f"Unsupported close outcome '{outcome}'. Expected one of: {allowed}")
    state = load_state(root)
    stop_summary: StopTaskSummary | None = None
    task_snapshot = get_task_record(root, task_id)
    if task_snapshot is None or task_snapshot.runtime.execution.active_subagent is None:
        active_subagent_pid = None
    else:
        active_subagent_pid = task_snapshot.runtime.execution.active_subagent.pid
    if runner_lock_is_held(root):
        runner_metadata = read_runner_lock_metadata(root)
    else:
        runner_metadata = None
    if state.active_task_id == task_id or (runner_metadata is not None and runner_metadata.active_task_id == task_id):
        stop_summary = stop_current_task(root)
    if stop_summary is None:
        runner_pid = None
    else:
        runner_pid = stop_summary.runner_pid
    _terminate_subagent_pid(task_id, active_subagent_pid)
    _terminate_subagent_pid(task_id, runner_pid)
    with workspace_lock(root):
        task = get_task_record(root, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        before_task = snapshot_task_audit_state(task)
        if follow_up_task_id is not None:
            follow_up_task_id = follow_up_task_id.strip()
            if not follow_up_task_id:
                raise ValueError("Follow-up task id must not be empty")
            if follow_up_task_id == task.id:
                raise ValueError(f"Task {task.id} cannot reference itself as a follow-up task")
            if get_task_record(root, follow_up_task_id) is None:
                raise ValueError(f"Task {follow_up_task_id} not found")
        state = load_state(root)
        queue_before = list(state.queue)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status == TaskStatus.DONE:
            raise ValueError(f"Task {task.id} is already done and cannot be closed")
        journal_message = _apply_close_task_state(
            task,
            outcome=outcome,
            reason=reason,
            follow_up_task_id=follow_up_task_id,
        )
        drop_task_from_workspace_state(state, task.id)
        _persist_transition(
            root,
            task=task,
            state=state,
            journal_message=journal_message,
            action="closed",
            actor=audit_actor,
            source=audit_source,
            before_task=before_task,
            before_queue=queue_before,
            context={
                "outcome": outcome,
                "reason": reason,
                "follow_up_task_id": follow_up_task_id,
            },
        )
        _reset_pipeline_state(root, task.id)
        return task


def _park_task_transition(
    root: Path,
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
    with workspace_lock(root):
        task = require_task(root, task_id)
        before_task = snapshot_task_audit_state(task)
        state = load_state(root)
        queue_before = list(state.queue)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status == TaskStatus.DONE:
            raise ValueError(f"Task {task.id} is already done and cannot be parked")
        _apply_parked_task_state(task)
        drop_task_from_workspace_state(state, task.id)
        _persist_transition(
            root,
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


def abandon_task(
    root: Path,
    task_id: str,
    reason: str = "Task abandoned via CLI.",
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """
    Public CLI/agent entry for the operator-initiated kill path.

    Signals the live subagent and marks the task cancelled; thin wrapper
    around ``_abandon_task_transition`` so the public surface stays
    importable from ``litehive.tasks.status``.
    """
    return _abandon_task_transition(
        root,
        task_id,
        reason=reason,
        audit_actor=audit_actor,
        audit_source=audit_source,
    )


def close_task(
    root: Path,
    task_id: str,
    outcome: str,
    reason: str | None = None,
    follow_up_task_id: str | None = None,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """
    Public CLI/agent entry for terminating a task with a deliberate verdict.

    Accepts ``done``, ``wont_do``, ``deferred``, ``duplicate``, or
    ``execution_cancelled`` as outcome plus an optional follow-up task
    reference; thin wrapper around ``_close_task_transition`` so the public
    surface stays importable from ``litehive.tasks.status``.
    """
    return _close_task_transition(
        root,
        task_id,
        outcome=outcome,
        reason=reason,
        follow_up_task_id=follow_up_task_id,
        audit_actor=audit_actor,
        audit_source=audit_source,
    )


def park_task(
    root: Path,
    task_id: str,
    reason: str = "Task parked via CLI.",
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """
    Public CLI/agent entry for parking a task without closing it.

    Removes the task from the queue but keeps it resumable so the operator
    can pick it up again later via ``litehive task resume``; thin wrapper
    around ``_park_task_transition`` for the public surface.
    """
    return _park_task_transition(
        root,
        task_id,
        reason=reason,
        audit_actor=audit_actor,
        audit_source=audit_source,
    )
