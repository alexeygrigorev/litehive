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
        self.workspace = workspace

    def eligible_tasks(self) -> list[TaskRecord]:
        return [
            task
            for task in WorkspaceTasks(self.workspace).list(strict=False)
            if is_task_eligible_for_execution(task)
        ]

    def select_next(self) -> TaskSelection:
        return _dequeue_next_task_selection(self.workspace)

    def peek_next_selection(self) -> TaskSelection:
        return _peek_next_task_selection(self.workspace)

    def dequeue_next(self) -> TaskRecord | None:
        return _dequeue_next_task(self.workspace)

    def peek_next(self) -> TaskRecord | None:
        return _peek_next_task(self.workspace)

    def enqueue(self, task_id: str, front: bool = False) -> WorkspaceState:
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
        return self.remove_from_state(state, task_id)

    def mark_active(self, task_id: str | None) -> WorkspaceState:
        return _set_active_task(self.workspace, task_id)

    def clear_active(self) -> WorkspaceState:
        return _clear_active_task(self.workspace)

    def restore_untouched_active(self) -> WorkspaceState:
        return _restore_untouched_active_task(self.workspace)

    def active_task_markers(self, state: WorkspaceState | None = None) -> dict[str, list[str]]:
        return _active_task_markers_impl(self.workspace, state)

    def validate_single_active_task(self, state: WorkspaceState | None = None) -> None:
        _validate_single_active_task_impl(self.workspace, state)

    def validate_dependencies(self, task_id: str, depends_on: list[str]) -> None:
        TaskDependencyValidator(self.workspace).validate(task_id, depends_on)

    def is_resumable(self, task: TaskRecord) -> bool:
        return resumable_queue_stage(task) is not None

    def is_runnable(self, task: TaskRecord) -> bool:
        return is_task_eligible_for_execution(task)

    @staticmethod
    def remove_from_state(state: WorkspaceState, task_id: str) -> bool:
        return _drop_task_from_workspace_state_impl(state, task_id)

    @staticmethod
    def restore_missing_from_state(
        state: WorkspaceState,
        tasks_by_id: dict[str, TaskRecord],
    ) -> list[str]:
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
