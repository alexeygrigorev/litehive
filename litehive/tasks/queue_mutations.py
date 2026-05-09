"""Workspace-state mutations for the queue: enqueue, move, prioritize, and
recovery-related lifecycle resets.

These helpers all take or return ``WorkspaceState`` (or mutate ``TaskRecord``
in place) and persist via ``state.persist`` / ``workspace_lock``. Selection
logic lives in ``queue_selection``; pure predicates live in
``queue_eligibility``.
"""

from litehive.domain.common import PipelineStatus, TaskExecutionStatus, TaskStatus, utcnow
from litehive.domain.outcomes import TaskOutcomeKind
from litehive.domain.runtime import TaskOutcomeState
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.state.records import set_task_commit_sha, WorkspaceTasks
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.queue_eligibility import (
    _normalize_resumable_stage_name,
    resumable_queue_stage,
)
from litehive.tasks.runtime import clear_task_run_activity, idle_stage_state
from litehive.workspace import Workspace


def _prioritize_audit_entries(
    task_ids: list[str],
    queued_tasks: dict,
    before_tasks: dict,
    queue_before: list[str],
    queue_after: list[str],
) -> list:
    """
    Build the per-task ``queue_prioritized`` audit entries.

    Threads the before/after queue snapshots into each entry so
    diagnostics can reconstruct the exact queue change without
    looking up sibling rows. Caller:
    :func:`prioritize_queued_tasks`.
    """
    entries: list = []
    for task_id in task_ids:
        entries.append(
            build_task_audit_entry(
                task_id=task_id,
                action="queue_prioritized",
                actor="operator",
                source="queue",
                before_task=before_tasks[task_id],
                after_task=queued_tasks[task_id],
                before_queue=queue_before,
                after_queue=queue_after,
                context={"requested_order": list(task_ids)},
            )
        )
    return entries


def reset_task_for_recovery(
    task: TaskRecord,
    status: TaskStatus | str,
    pipeline_status: PipelineStatus | str,
    clear_last_outcome: bool = True,
) -> None:
    """
    Rewind a task's lifecycle cursor and clear runtime activity for a fresh attempt.

    Called by the status-mutation flows — ``requeue_task``, flagged-task
    auto-recovery, and the dequeue path that revives flagged tasks — so the
    runner sees a clean ``idle`` stage marker and zeroed retry counters
    instead of leftover ``running``/``failed`` state from the previous run.
    """
    now = utcnow()
    canonical_status = status if isinstance(status, TaskStatus) else TaskStatus(status)
    canonical_pipeline_status = (
        pipeline_status if isinstance(pipeline_status, PipelineStatus) else PipelineStatus(pipeline_status)
    )
    task.status = canonical_status
    task.close_reason = None
    task.flag_reason = None
    task.pipeline_status = canonical_pipeline_status
    clear_task_run_activity(task, execution_status=TaskExecutionStatus.IDLE, updated_at=now, clear_interruption=True)
    task.runtime.pipeline.retry_count = 0
    task.runtime.pipeline.retry_limit = 0
    task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now, stage=canonical_pipeline_status)
    if clear_last_outcome:
        task.runtime.pipeline.last_outcome = TaskOutcomeState()
    else:
        last_outcome = task.runtime.pipeline.last_outcome
        if last_outcome.kind == TaskOutcomeKind.INTERRUPTED:
            task.runtime.pipeline.last_outcome = last_outcome.model_copy(
                update={"stage": canonical_pipeline_status}
            )


def enqueue_recovered_task(state: WorkspaceState, task_id: str) -> None:
    """
    Move a recovered task to the back of the queue, exactly once.

    Recovery flows — ``TaskQueueService.restore_untouched_active`` and the flagged-task
    auto-recovery in ``TaskQueueService.select_next`` — call this so a task
    that was rolled back to ``queued`` lands at the tail without showing up
    twice if it was already present in the queue.
    """
    state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
    state.queue.append(task_id)


def drop_task_from_workspace_state(state: WorkspaceState, task_id: str) -> bool:
    """
    Remove a task from every workspace-state slot that can reference it.

    Wipes the queue, the active_task_id pointer, and the unmerged-worktrees
    list in one pass so a closed/abandoned/parked task cannot be referenced
    from one slot after being removed from another. Returns True when at
    least one slot was modified so callers can decide whether to persist.
    """
    changed = False
    if state.active_task_id == task_id:
        state.active_task_id = None
        changed = True
    if task_id in state.queue:
        state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
        changed = True
    original_unmerged = len(state.unmerged_worktrees)
    state.unmerged_worktrees = [item for item in state.unmerged_worktrees if item.task_id != task_id]
    return changed or len(state.unmerged_worktrees) != original_unmerged


def prepare_completed_task_for_recovery(task: TaskRecord, recovery_stage: str) -> None:
    """
    Reopen a finished task at the chosen stage.

    Called by the completed-task recovery flow (``litehive task recover``
    on an already-done task): clearing the merge commit reference is what
    lets the runner re-pick the task from the chosen pipeline stage rather
    than treating it as already merged.
    """
    reset_task_for_recovery(
        task,
        status=TaskStatus.QUEUED,
        pipeline_status=recovery_stage,
    )
    set_task_commit_sha(task, None)


def canonicalize_resumable_queue_task(task: TaskRecord, stage: str | None = None) -> str | None:
    """
    Force a resumable task into a clean ``queued`` shape at the chosen stage.

    Called by workspace-repair and stale-runner recovery once they have
    decided a task is recoverable: strips ``flag_reason``/``close_reason``,
    plants an ``idle`` stage marker, and re-points any sticky ``interrupted``
    outcome at the resume stage so the runner picks the task up cleanly
    instead of inheriting stale failure state.
    """
    if stage is not None:
        target_stage = _normalize_resumable_stage_name(stage)
    else:
        target_stage = resumable_queue_stage(task)
    if target_stage is None:
        return None
    now = clear_task_run_activity(task, execution_status=TaskExecutionStatus.IDLE)
    task.status = TaskStatus.QUEUED
    task.close_reason = None
    task.flag_reason = None
    task.pipeline_status = PipelineStatus(target_stage)
    task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now, stage=target_stage)
    last_outcome = task.runtime.pipeline.last_outcome
    if last_outcome.kind == TaskOutcomeKind.INTERRUPTED:
        task.runtime.pipeline.last_outcome = last_outcome.model_copy(update={"stage": target_stage})
    return target_stage
