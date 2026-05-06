"""Public surface for the queue subsystem.

The implementation is split across sibling modules:

* ``litehive.tasks.queue_eligibility`` — pure predicates and stage helpers.
* ``litehive.tasks.queue_mutations`` — workspace-state mutations
  (enqueue, move, prioritize, reset/recovery resets).
* ``litehive.tasks.queue_selection`` — dequeue/peek, active-task
  pinning, runtime-store interactions, and the single-active-task invariant.

This module re-exports the names existing call sites import from
``litehive.tasks.queue`` so the public API stays stable.
"""

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
    is_task_eligible_for_execution,
    resumable_queue_stage,
    resumable_running_stage,
    task_has_resume_marker,
    validate_task_dependencies,
    validate_task_dependencies_for_workspace,
)
from litehive.tasks.queue_mutations import (
    _enqueue_task,
    canonicalize_resumable_queue_task,
    drop_task_from_workspace_state,
    enqueue_recovered_task,
    enqueue_task,
    enqueue_task_front,
    enqueue_task_for_workspace,
    enqueue_task_front_for_workspace,
    move_queued_task,
    move_queued_task_for_workspace,
    prepare_completed_task_for_recovery,
    prioritize_queued_tasks,
    prioritize_queued_tasks_for_workspace,
    reset_task_for_recovery,
)
from litehive.tasks.queue_selection import (
    _dependent_task_count,
    _normalize_stale_pipeline_statuses,
    _resolve_next_task_from_snapshot,
    _resolve_next_task_from_state,
    _task_selection_key,
    active_task_markers,
    active_task_markers_for_workspace,
    clear_active_task,
    dequeue_next_task,
    dequeue_next_task_selection,
    peek_next_task,
    peek_next_task_selection,
    restore_missing_queued_tasks,
    restore_untouched_active_task,
    set_active_task,
    validate_single_active_task,
    validate_single_active_task_for_workspace,
)

# ``idle_stage_state`` is defined in ``litehive.tasks.runtime`` but external
# callers (``recovery.workspace_repair``) historically imported it from this
# module, so we keep it re-exported here.
from litehive.tasks.runtime import idle_stage_state

__all__ = [
    "_RESUMABLE_PIPELINE_STAGES",
    "_TERMINAL_EXECUTION_STATUSES",
    "_TERMINAL_OUTCOME_KINDS",
    "_TRUSTED_STAGE_MARKER_STATUSES",
    "_auto_recovery_stage_for_flagged_task",
    "_dependency_reaches_task",
    "_dependent_task_count",
    "_enqueue_task",
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
    "active_task_markers",
    "active_task_markers_for_workspace",
    "canonicalize_resumable_queue_task",
    "clear_active_task",
    "dequeue_next_task",
    "dequeue_next_task_selection",
    "drop_task_from_workspace_state",
    "enqueue_recovered_task",
    "enqueue_task",
    "enqueue_task_front",
    "enqueue_task_for_workspace",
    "enqueue_task_front_for_workspace",
    "idle_stage_state",
    "is_task_eligible_for_execution",
    "move_queued_task",
    "move_queued_task_for_workspace",
    "peek_next_task",
    "peek_next_task_selection",
    "prepare_completed_task_for_recovery",
    "prioritize_queued_tasks",
    "prioritize_queued_tasks_for_workspace",
    "reset_task_for_recovery",
    "restore_missing_queued_tasks",
    "restore_untouched_active_task",
    "resumable_queue_stage",
    "resumable_running_stage",
    "set_active_task",
    "task_has_resume_marker",
    "validate_single_active_task",
    "validate_single_active_task_for_workspace",
    "validate_task_dependencies",
    "validate_task_dependencies_for_workspace",
]
