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
from litehive.state.locking import (
    ensure_future_task_mutation_allowed_for_workspace,
    workspace_lock,
)
from litehive.state.persist import (
    load_state_for_workspace,
    save_state_without_runner_guard_for_workspace,
)
from litehive.state.records import set_task_commit_sha
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.queue_eligibility import (
    _normalize_resumable_stage_name,
    resumable_queue_stage,
)
from litehive.tasks.runtime import clear_task_run_activity, idle_stage_state
from litehive.workspace import Workspace


def enqueue_task_for_workspace(workspace: Workspace, task_id: str) -> WorkspaceState:
    """
    Append a task to the back of the workspace queue.
    """
    return _enqueue_task_for_workspace(workspace, task_id, front=False)


def enqueue_task_front_for_workspace(workspace: Workspace, task_id: str) -> WorkspaceState:
    """
    Insert a task at the head of the workspace queue.
    """
    return _enqueue_task_for_workspace(workspace, task_id, front=True)


def _enqueue_task_for_workspace(workspace: Workspace, task_id: str, front: bool) -> WorkspaceState:
    """
    Shared body of ``enqueue_task`` / ``enqueue_task_front``.

    Dedupes, inserts, and audits in one pass so the two public wrappers
    share locking, audit-entry shape, and runner-guard handling. Both
    wrappers are currently dead code, but this helper is preserved alongside
    them in case they are re-introduced.
    """
    root = workspace.root
    with workspace_lock(root):
        state = load_state_for_workspace(workspace)
        ensure_future_task_mutation_allowed_for_workspace(workspace, [task_id], state=state)
        task = workspace.require_task(task_id)
        before_task = snapshot_task_audit_state(task)
        queue_before = list(state.queue)
        state.queue = [item for item in state.queue if item != task_id]
        if front:
            state.queue.insert(0, task_id)
        else:
            state.queue.append(task_id)
        save_state_without_runner_guard_for_workspace(
            workspace,
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


def move_queued_task_for_workspace(workspace: Workspace, task_id: str, position: int) -> WorkspaceState:
    """
    Reorder a queued task to a 1-based position and record an audit entry.

    The ``litehive queue move`` CLI calls this when an operator hand-curates
    the queue; the engine-switch flow also re-positions the active task
    here when the user swaps engines mid-run so the swapped task continues
    next instead of being preempted by other queued work.
    """
    if position < 1:
        raise ValueError("Queue position must be 1 or greater")
    root = workspace.root
    with workspace_lock(root):
        state = load_state_for_workspace(workspace)
        ensure_future_task_mutation_allowed_for_workspace(workspace, [task_id], state=state)
        task = workspace.require_task(task_id)
        before_task = snapshot_task_audit_state(task)
        queue_before = list(state.queue)
        if task_id not in state.queue:
            raise ValueError(f"Task {task_id} is not queued")
        queue = [item for item in state.queue if item != task_id]
        target_index = min(position - 1, len(queue))
        queue.insert(target_index, task_id)
        state.queue = queue
        save_state_without_runner_guard_for_workspace(
            workspace,
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


def prioritize_queued_tasks_for_workspace(workspace: Workspace, task_ids: list[str]) -> WorkspaceState:
    """
    Hoist the given queued tasks to the front of the queue, in order.

    Called by the ``litehive queue prioritize`` CLI when an operator wants
    a specific batch run next without manually moving each task one at a
    time; the relative ordering inside ``task_ids`` is preserved so the
    operator can also reshuffle a small set in one call.
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
    root = workspace.root
    with workspace_lock(root):
        state = load_state_for_workspace(workspace)
        ensure_future_task_mutation_allowed_for_workspace(workspace, task_ids, state=state)
        queue_before = list(state.queue)
        missing = [task_id for task_id in task_ids if task_id not in state.queue]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"Tasks are not queued: {joined}")
        queued_tasks = {task_id: workspace.require_task(task_id) for task_id in task_ids}
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
        save_state_without_runner_guard_for_workspace(
            workspace,
            state,
            audit_entries=audit_entries,
        )
        return state


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

    Recovery flows — ``restore_untouched_active_task`` and the flagged-task
    auto-recovery in ``dequeue_next_task_selection`` — call this so a task
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
