"""Public surface for the queue subsystem.

The implementation is split across sibling modules:

* ``litehive.tasks.queue_eligibility`` — pure predicates and stage helpers.
* ``litehive.tasks.queue_mutations`` — workspace-state mutations
  (enqueue, move, prioritize, reset/recovery resets).
* ``litehive.tasks.queue_selection`` — dequeue/peek, active-task
  pinning, runtime-store interactions, and the single-active-task invariant.

This module keeps the public API stable while production callers migrate
toward the workspace-bound ``TaskQueueService``.
"""

from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.domain.task_ops import TaskSelection
from litehive.state.locking import WorkspaceMutationGuard, WorkspaceStateLock
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import WorkspaceTasks
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.queue_eligibility import (
    _RESUMABLE_PIPELINE_STAGES,
    _TERMINAL_EXECUTION_STATUSES,
    _TERMINAL_OUTCOME_KINDS,
    _TRUSTED_STAGE_MARKER_STATUSES,
    _auto_recovery_stage_for_flagged_task,
    _dependency_reaches_task,
    _has_terminal_execution_status,
    _has_terminal_outcome_kind,
    _is_interrupted_task,
    _is_parked_task,
    _is_recovery_budget_exhausted,
    _is_task_completed,
    _live_active_pipeline_stage,
    _needs_manual_intervention,
    _normalize_resumable_stage_name,
    _should_requeue_commit_stage_task,
    _task_blockers,
    TaskDependencyValidator,
    is_task_eligible_for_execution,
    resumable_queue_stage,
    resumable_running_stage,
    task_has_resume_marker,
)
from litehive.tasks.queue_mutations import (
    canonicalize_resumable_queue_task,
    drop_task_from_workspace_state as _drop_task_from_workspace_state_impl,
    enqueue_recovered_task,
    prepare_completed_task_for_recovery,
    _prioritize_audit_entries,
    reset_task_for_recovery,
)
from litehive.tasks.queue_selection import (
    _active_task_markers_impl,
    _clear_active_task,
    _dequeue_next_task,
    _dequeue_next_task_selection,
    _dependent_task_count,
    _normalize_stale_pipeline_statuses,
    _peek_next_task,
    _peek_next_task_selection,
    _resolve_next_task_from_snapshot,
    _resolve_next_task_from_state,
    _restore_untouched_active_task,
    _set_active_task,
    _task_selection_key,
    restore_missing_queued_tasks as _restore_missing_queued_tasks_impl,
    _validate_single_active_task_impl,
)

# ``idle_stage_state`` is defined in ``litehive.tasks.runtime`` but external
# callers (``recovery.workspace_repair``) historically imported it from this
# module, so we keep it re-exported here.
from litehive.tasks.runtime import idle_stage_state
from litehive.workspace import Workspace


class TaskQueueService:
    """
    Workspace-bound owner for queue selection and queue mutations.

    The lower-level modules still hold the implementation bodies during the
    migration, but public queue entry points bind through this service so
    callers can move away from workspace-first free functions incrementally.
    """

    def __init__(self, workspace: Workspace) -> None:
        """
        Bind the service to a workspace whose queue will be managed.

        The workspace is forwarded to lower-level selection and mutation
        helpers; the service itself holds no mutable state.
        """
        self.workspace = workspace

    def eligible_tasks(self) -> list[TaskRecord]:
        """
        Return all tasks that the runner is allowed to pick up right now.

        A task is eligible when its status, pipeline stage, dependency
        graph, and recovery budget all allow execution.  The list is not
        ordered by queue position.
        """
        return [
            task for task in WorkspaceTasks(self.workspace).list(strict=False) if is_task_eligible_for_execution(task)
        ]

    def select_next(self) -> TaskSelection:
        """
        Dequeue the highest-priority eligible task and mark it active.

        Returns a ``TaskSelection`` describing the chosen task and the
        workspace-state changes, or an empty selection when nothing is
        eligible.
        """
        return _dequeue_next_task_selection(self.workspace)

    def peek_next_selection(self) -> TaskSelection:
        """
        Preview which task the next dequeue would select, without mutating.

        Useful for dry-run displays and pre-flight checks that need to know
        what the runner would pick up.
        """
        return _peek_next_task_selection(self.workspace)

    def dequeue_next(self) -> TaskRecord | None:
        """
        Dequeue and return the next eligible task, or None if the queue is empty.

        Simpler alternative to ``select_next`` for callers that only need the
        task record.
        """
        return _dequeue_next_task(self.workspace)

    def peek_next(self) -> TaskRecord | None:
        """
        Preview the next task without removing it from the queue.

        Returns None when no eligible task exists.
        """
        return _peek_next_task(self.workspace)

    def enqueue(self, task_id: str, front: bool = False) -> WorkspaceState:
        """
        Add a task to the end (or front) of the workspace queue.

        Holds the workspace-state lock and validates that the task is in a
        future-ready status before inserting.  An audit entry records the
        enqueue, including whether the task was placed at the front.
        """
        with WorkspaceStateLock(self.workspace).hold():
            state = WorkspaceStateRepository(self.workspace).load()
            WorkspaceMutationGuard(self.workspace).ensure_future_task_mutation_allowed([task_id], state=state)
            task = WorkspaceTasks(self.workspace).require(task_id)
            before_task = snapshot_task_audit_state(task)
            queue_before = list(state.queue)
            state.queue = [item for item in state.queue if item != task_id]
            if front:
                state.queue.insert(0, task_id)
            else:
                state.queue.append(task_id)
            WorkspaceStateRepository(self.workspace).save_without_runner_guard(
                state,
                audit_entries=[
                    build_task_audit_entry(
                        task_id=task_id,
                        action="queue_enqueued",
                        actor="operator",
                        source="queue",
                        before_task=before_task,
                        after_task=task,
                        before_queue=queue_before,
                        after_queue=state.queue,
                        context={"front": front},
                    )
                ],
            )
            return state

    def move(self, task_id: str, position: int) -> WorkspaceState:
        """
        Move a queued task to a 1-indexed position in the queue.

        Position 1 is the front of the queue.  If the requested position
        exceeds the queue length the task is placed at the end.
        """
        if position < 1:
            raise ValueError("Queue position must be 1 or greater")
        with WorkspaceStateLock(self.workspace).hold():
            state = WorkspaceStateRepository(self.workspace).load()
            WorkspaceMutationGuard(self.workspace).ensure_future_task_mutation_allowed([task_id], state=state)
            task = WorkspaceTasks(self.workspace).require(task_id)
            before_task = snapshot_task_audit_state(task)
            queue_before = list(state.queue)
            if task_id not in state.queue:
                raise ValueError(f"Task {task_id} is not queued")
            queue = [item for item in state.queue if item != task_id]
            target_index = min(position - 1, len(queue))
            queue.insert(target_index, task_id)
            state.queue = queue
            WorkspaceStateRepository(self.workspace).save_without_runner_guard(
                state,
                audit_entries=[
                    build_task_audit_entry(
                        task_id=task_id,
                        action="queue_moved",
                        actor="operator",
                        source="queue",
                        before_task=before_task,
                        after_task=task,
                        before_queue=queue_before,
                        after_queue=state.queue,
                        context={"requested_position": position},
                    )
                ],
            )
            return state

    def prioritize(self, task_ids: list[str]) -> WorkspaceState:
        """
        Promote a set of tasks to the front of the queue in the given order.

        All listed tasks must already be queued.  Duplicates are rejected so
        the resulting queue order is unambiguous.
        """
        if not task_ids:
            raise ValueError("At least one task id is required")
        seen: set[str] = set()
        duplicates: set[str] = set()
        for task_id in task_ids:
            if task_id in seen:
                duplicates.add(task_id)
                continue
            seen.add(task_id)
        if duplicates:
            joined = ", ".join(sorted(duplicates))
            raise ValueError(f"Task ids must be unique: {joined}")
        with WorkspaceStateLock(self.workspace).hold():
            state = WorkspaceStateRepository(self.workspace).load()
            WorkspaceMutationGuard(self.workspace).ensure_future_task_mutation_allowed(task_ids, state=state)
            queue_before = list(state.queue)
            missing = [task_id for task_id in task_ids if task_id not in state.queue]
            if missing:
                joined = ", ".join(missing)
                raise ValueError(f"Tasks are not queued: {joined}")
            tasks = WorkspaceTasks(self.workspace)
            queued_tasks = {task_id: tasks.require(task_id) for task_id in task_ids}
            before_tasks = {task_id: snapshot_task_audit_state(task) for task_id, task in queued_tasks.items()}
            remaining = [queued_id for queued_id in state.queue if queued_id not in task_ids]
            state.queue = [*task_ids, *remaining]
            audit_entries = _prioritize_audit_entries(
                task_ids=task_ids,
                queued_tasks=queued_tasks,
                before_tasks=before_tasks,
                queue_before=queue_before,
                queue_after=state.queue,
            )
            WorkspaceStateRepository(self.workspace).save_without_runner_guard(state, audit_entries=audit_entries)
            return state

    def remove(self, state: WorkspaceState, task_id: str) -> bool:
        """
        Remove a task from the in-memory workspace state's queue list.

        Delegates to ``remove_from_state``; returns True if the task was
        present and removed, False otherwise.
        """
        return self.remove_from_state(state, task_id)

    def mark_active(self, task_id: str | None) -> WorkspaceState:
        """
        Pin a task as the workspace's active task, or clear the pin.

        Passing None clears the active-task slot.  The active-task marker
        prevents the dequeue loop from starting a second task concurrently.
        """
        return _set_active_task(self.workspace, task_id)

    def clear_active(self) -> WorkspaceState:
        """
        Unpin whichever task is currently marked active.

        Safe to call when no task is active; the state is returned unchanged.
        """
        return _clear_active_task(self.workspace)

    def restore_untouched_active(self) -> WorkspaceState:
        """
        Re-activate the previously active task if it was not modified.

        Used by the dequeue loop after a peek-and-check cycle to confirm the
        task is still eligible before committing to a run.
        """
        return _restore_untouched_active_task(self.workspace)

    def active_task_markers(self, state: WorkspaceState | None = None) -> dict[str, list[str]]:
        """
        Collect runtime markers that indicate which task(s) are active.

        Returns a dict keyed by marker type (e.g. active_task_id) whose values
        are lists of task ids carrying that marker.  Primarily a diagnostic
        helper for the single-active-task invariant check.
        """
        return _active_task_markers_impl(self.workspace, state)

    def validate_single_active_task(self, state: WorkspaceState | None = None) -> None:
        """
        Assert that at most one task is marked active across all markers.

        Raises if multiple tasks claim the active slot, which would indicate a
        bug in the dequeue or recovery logic.
        """
        _validate_single_active_task_impl(self.workspace, state)

    def validate_dependencies(self, task_id: str, depends_on: list[str]) -> None:
        """
        Check that a task's dependency list is acyclic and references real tasks.

        Raises on cycles, missing tasks, or self-dependencies.
        """
        TaskDependencyValidator(self.workspace).validate(task_id, depends_on)

    def is_resumable(self, task: TaskRecord) -> bool:
        """
        True when the task has a pipeline stage that supports resumption.

        A resumable task can be placed back on the queue at the stage it
        occupied when it was interrupted, rather than starting over.
        """
        return resumable_queue_stage(task) is not None

    def is_runnable(self, task: TaskRecord) -> bool:
        """
        True when the task passes all eligibility checks for execution.

        Combines status, dependency, recovery-budget, and interruption
        predicates into a single boolean.
        """
        return is_task_eligible_for_execution(task)

    @staticmethod
    def remove_from_state(state: WorkspaceState, task_id: str) -> bool:
        """
        Drop a task id from the in-memory queue list, returning True if removed.

        Pure in-memory operation on the state object; does not persist.
        """
        return _drop_task_from_workspace_state_impl(state, task_id)

    @staticmethod
    def restore_missing_from_state(
        state: WorkspaceState,
        tasks_by_id: dict[str, TaskRecord],
    ) -> list[str]:
        """
        Re-insert queued task ids that were lost from the state's queue list.

        Compares the persisted queue against ``tasks_by_id`` and appends any
        task that exists in the workspace but has no queue entry.  Returns the
        list of restored task ids.
        """
        return _restore_missing_queued_tasks_impl(state, tasks_by_id)


__all__ = [
    "_RESUMABLE_PIPELINE_STAGES",
    "_TERMINAL_EXECUTION_STATUSES",
    "_TERMINAL_OUTCOME_KINDS",
    "_TRUSTED_STAGE_MARKER_STATUSES",
    "_auto_recovery_stage_for_flagged_task",
    "_dependency_reaches_task",
    "_dependent_task_count",
    "_has_terminal_execution_status",
    "_has_terminal_outcome_kind",
    "_is_interrupted_task",
    "_is_parked_task",
    "_is_recovery_budget_exhausted",
    "_is_task_completed",
    "_live_active_pipeline_stage",
    "_needs_manual_intervention",
    "_normalize_resumable_stage_name",
    "_normalize_stale_pipeline_statuses",
    "_resolve_next_task_from_snapshot",
    "_resolve_next_task_from_state",
    "_should_requeue_commit_stage_task",
    "_task_blockers",
    "_task_selection_key",
    "canonicalize_resumable_queue_task",
    "enqueue_recovered_task",
    "idle_stage_state",
    "is_task_eligible_for_execution",
    "prepare_completed_task_for_recovery",
    "reset_task_for_recovery",
    "resumable_queue_stage",
    "resumable_running_stage",
    "task_has_resume_marker",
    "TaskQueueService",
    "TaskDependencyValidator",
]
