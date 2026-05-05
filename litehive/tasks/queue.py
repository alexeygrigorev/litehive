"""Queue operations: list management (enqueue/move/prioritize) and selection
logic (dequeue, block, dep-resolve)."""

import logging
from pathlib import Path

from litehive.domain.common import PipelineStatus, TaskStage, TaskStatus, utcnow
from litehive.domain.reports import RecoveryAction
from litehive.domain.recovery import TriggerEventKind
from litehive.domain.runtime import TaskOutcomeState
from litehive.domain.task import TaskRecord, WorkspaceState

from litehive.recovery.detection import TaskLaunchFailure
from litehive.recovery.execution_recovery import (
    interruption_journal_message,
    prepare_interrupted_task,
    recover_stale_runner_state,
    stale_interruption_reason,
)
from litehive.state.locking import (
    ensure_future_task_mutation_allowed,
    workspace_lock,
    workspace_mutation_guard,
)
from litehive.state.persist import (
    load_state,
    persist_task_and_state,
    persist_tasks_and_state,
    save_state,
    save_state_without_runner_guard,
)
from litehive.state.records import (
    get_task,
    list_tasks,
    require_task,
    set_task_commit_sha,
)
from litehive.state.store import runtime_store
from litehive.domain.task_ops import BlockedTask, TaskPlan, TaskSelection, WorkspaceConflictError
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.failed_runs import has_blocking_failed_run_history
from litehive.tasks.normalization import implementation_entry_stage
from litehive.tasks.recovery_reports import record_recovery_report
from litehive.tasks.runtime import clear_task_run_activity, idle_stage_state

logger = logging.getLogger(__name__)

_TERMINAL_EXECUTION_STATUSES = {"done", "cancelled", "failed", "blocked", "interrupted"}
_TERMINAL_OUTCOME_KINDS = {"closed", "duplicate", "deferred", "wont_do"}
_TRUSTED_STAGE_MARKER_STATUSES = {"idle", "paused", "interrupted", "running"}
_RESUMABLE_PIPELINE_STAGES: frozenset[TaskStage] = frozenset(
    {TaskStage.GROOMING, TaskStage.IMPLEMENTING, TaskStage.TESTING, TaskStage.ACCEPTING, TaskStage.COMMIT_TO_GIT}
)


# --- list ops ---


def enqueue_task(root: Path, task_id: str) -> WorkspaceState:
    """Append a task to the back of the workspace queue.

    No production or test callers remain — candidate for removal alongside
    ``enqueue_task_front``; queue inserts now go through ``move_queued_task``
    and ``prioritize_queued_tasks`` from the queue CLI.
    """
    return _enqueue_task(root, task_id, front=False)


def enqueue_task_front(root: Path, task_id: str) -> WorkspaceState:
    """Insert a task at the head of the workspace queue.

    No callers — see ``enqueue_task``; both wrappers look like dead weight.
    """
    return _enqueue_task(root, task_id, front=True)


def _enqueue_task(root: Path, task_id: str, front: bool) -> WorkspaceState:

    with workspace_lock(root):
        state = load_state(root)
        ensure_future_task_mutation_allowed(root, [task_id], state=state)
        task = require_task(root, task_id)
        before_task = snapshot_task_audit_state(task)
        queue_before = list(state.queue)
        state.queue = [item for item in state.queue if item != task_id]
        if front:
            state.queue.insert(0, task_id)
        else:
            state.queue.append(task_id)
        save_state_without_runner_guard(
            root,
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


def move_queued_task(root: Path, task_id: str, position: int) -> WorkspaceState:
    """Reorder a queued task to a 1-based position, recording an audit entry.

    The ``litehive queue move`` CLI calls this when an operator hand-curates the
    queue; the engine switch flow also re-positions the active task here when
    the user swaps engines mid-run.
    """
    if position < 1:
        raise ValueError("Queue position must be 1 or greater")
    with workspace_lock(root):
        state = load_state(root)
        ensure_future_task_mutation_allowed(root, [task_id], state=state)
        task = require_task(root, task_id)
        before_task = snapshot_task_audit_state(task)
        queue_before = list(state.queue)
        if task_id not in state.queue:
            raise ValueError(f"Task {task_id} is not queued")
        queue = [item for item in state.queue if item != task_id]
        target_index = min(position - 1, len(queue))
        queue.insert(target_index, task_id)
        state.queue = queue
        save_state_without_runner_guard(
            root,
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


def prioritize_queued_tasks(root: Path, task_ids: list[str]) -> WorkspaceState:
    """Hoist the given queued tasks to the front of the queue, in order.

    Called by the ``litehive queue prioritize`` CLI when an operator wants a
    specific batch run next without manually moving each task one at a time.
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
    with workspace_lock(root):
        state = load_state(root)
        ensure_future_task_mutation_allowed(root, task_ids, state=state)
        queue_before = list(state.queue)
        missing = [task_id for task_id in task_ids if task_id not in state.queue]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"Tasks are not queued: {joined}")
        queued_tasks = {task_id: require_task(root, task_id) for task_id in task_ids}
        before_tasks = {task_id: snapshot_task_audit_state(task) for task_id, task in queued_tasks.items()}
        remaining = [queued_id for queued_id in state.queue if queued_id not in task_ids]
        state.queue = [*task_ids, *remaining]
        save_state_without_runner_guard(
            root,
            state,
            audit_entries=[
                build_task_audit_entry(
                    task_id=task_id,
                    action="queue_prioritized",
                    actor="operator",
                    source="queue",
                    before_task=before_tasks[task_id],
                    after_task=queued_tasks[task_id],
                    before_queue=queue_before,
                    after_queue=state.queue,
                    context={"requested_order": list(task_ids)},
                )
                for task_id in task_ids
            ],
        )
        return state


def reset_task_for_recovery(
    task: TaskRecord,
    status: str,
    pipeline_status: str,
    clear_last_outcome: bool = True,
) -> None:
    """Rewind a task's lifecycle cursor and clear runtime activity for a fresh attempt.

    The status-mutation flows (``requeue_task``, flagged-task auto-recovery,
    and the dequeue path that revives flagged tasks) call this so the runner
    sees a clean ``idle`` stage marker and zeroed retry counters instead of
    leftover ``running``/``failed`` state from the previous attempt.
    """
    now = utcnow()
    task.status = status
    task.close_reason = None
    task.flag_reason = None
    task.pipeline_status = pipeline_status
    clear_task_run_activity(task, execution_status="idle", updated_at=now, clear_interruption=True)
    task.runtime.pipeline.retry_count = 0
    task.runtime.pipeline.retry_limit = 0
    task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now, stage=pipeline_status)
    if clear_last_outcome:
        task.runtime.pipeline.last_outcome = TaskOutcomeState()
    elif task.runtime.pipeline.last_outcome.kind == "interrupted":
        task.runtime.pipeline.last_outcome.stage = pipeline_status


def enqueue_recovered_task(state: WorkspaceState, task_id: str) -> None:
    state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
    state.queue.append(task_id)


def drop_task_from_workspace_state(state: WorkspaceState, task_id: str) -> bool:
    """Remove a task from workspace state (queue, active_task_id, unmerged_worktrees).

    Returns True if any state was modified, False otherwise.
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
    """Reopen a finished task at the chosen stage by resetting its lifecycle and dropping the merge SHA.

    The completed-task recovery flow (``litehive task recover`` after a task is
    already ``done``) calls this so the merge commit reference is cleared and
    the runner re-picks the task from the chosen pipeline stage.
    """
    reset_task_for_recovery(
        task,
        status="queued",
        pipeline_status=recovery_stage,
    )
    set_task_commit_sha(task, None)


# --- selection ---


def _normalize_resumable_stage_name(stage: str | None) -> str | None:
    if stage in _RESUMABLE_PIPELINE_STAGES:
        return stage
    return None


def resumable_queue_stage(task: TaskRecord) -> str | None:
    """Pick the pipeline stage a queued/idle task should resume at, or ``None`` if it must restart.

    Used by execution recovery and the ``resume`` lifecycle command to decide
    whether an interrupted task can pick up mid-pipeline (grooming through
    commit) instead of going back to ``backlog``.
    """
    interruption = task.runtime.execution.interruption
    current_stage = task.runtime.pipeline.current_stage
    candidates = [
        task.pipeline_status,
        None if interruption is None else interruption.resume_stage,
        None if interruption is None else interruption.pipeline_status,
    ]
    if current_stage.status in _TRUSTED_STAGE_MARKER_STATUSES:
        candidates.append(current_stage.stage)
    for candidate in candidates:
        normalized = _normalize_resumable_stage_name(None if candidate is None else str(candidate))
        if normalized is not None:
            return normalized
    return None


def resumable_running_stage(task: TaskRecord) -> str | None:
    """Pick the resume stage for a task whose runtime still claims to be ``running``.

    Stale-runner recovery calls this when it has just decided a "running" task
    is actually orphaned (subagent PID dead): it trusts the live current-stage
    marker first, then falls back to the queued-stage heuristics.
    """
    current_stage = task.runtime.pipeline.current_stage
    if current_stage.status == "running":
        normalized = _normalize_resumable_stage_name(None if current_stage.stage is None else str(current_stage.stage))
        if normalized is not None:
            return normalized
    return resumable_queue_stage(task)


def canonicalize_resumable_queue_task(task: TaskRecord, stage: str | None = None) -> str | None:
    """Force a resumable task into a clean ``queued`` shape at the chosen pipeline stage.

    Workspace-repair and stale-runner recovery call this once they have decided
    a task is recoverable: it strips ``flag_reason``/``close_reason``, plants an
    ``idle`` stage marker, and re-points any sticky ``interrupted`` outcome at
    the resume stage so the runner can pick the task up cleanly.
    """
    if stage is not None:
        target_stage = _normalize_resumable_stage_name(stage)
    else:
        target_stage = resumable_queue_stage(task)
    if target_stage is None:
        return None
    now = clear_task_run_activity(task, execution_status="idle")
    task.status = TaskStatus.QUEUED
    task.close_reason = None
    task.flag_reason = None
    task.pipeline_status = target_stage
    task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now, stage=target_stage)
    if task.runtime.pipeline.last_outcome.kind == "interrupted":
        task.runtime.pipeline.last_outcome.stage = target_stage
    return target_stage


def _needs_manual_intervention(task: TaskRecord) -> bool:
    return has_blocking_failed_run_history(task) or (
        task.status == TaskStatus.FLAGGED
        and (
            task.flag_count >= 3
            or task.flag_reason
            in {
                "hook_reject_loop",
                "merge_failed",
                "rejection_loop_detected",
                "semantic_reject",
                "time_budget_exceeded",
            }
        )
    )


def _is_recovery_budget_exhausted(task: TaskRecord) -> bool:
    return task.status == TaskStatus.FLAGGED and task.flag_reason in {
        "crash_budget_exhausted",
        "recovery_budget_exhausted",
        "recovery_failed",
    }


def _should_requeue_commit_stage_task(task: TaskRecord) -> bool:
    return task.pipeline_status == PipelineStatus.COMMIT_TO_GIT and task.status in {
        TaskStatus.QUEUED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.INTERRUPTED,
    }


def _has_terminal_execution_status(task: TaskRecord) -> bool:
    return str(task.runtime.pipeline.execution_status) in _TERMINAL_EXECUTION_STATUSES


def _has_terminal_outcome_kind(task: TaskRecord) -> bool:
    kind = task.runtime.pipeline.last_outcome.kind
    return kind is not None and str(kind) in _TERMINAL_OUTCOME_KINDS


def _live_active_pipeline_stage(state: WorkspaceState, tasks_by_id: dict[str, TaskRecord]) -> str | None:
    if state.active_task_id is None:
        return None
    active_task = tasks_by_id.get(state.active_task_id)
    if active_task is None:
        return None
    current_stage = active_task.runtime.pipeline.current_stage
    if str(active_task.runtime.pipeline.execution_status) == "running" or current_stage.status == "running":
        return str(current_stage.stage or active_task.pipeline_status)
    return None


def task_has_resume_marker(task: TaskRecord) -> bool:
    """Tell whether a task's runtime still vouches for its declared pipeline stage.

    The stale-pipeline normalizer (and the workspace-repair pass that mirrors
    it) calls this before demoting a queued task back to ``backlog`` — a task
    with a trustworthy stage marker or a matching interruption record must be
    left alone so it can resume in place.
    """
    stage = str(task.pipeline_status)
    current_stage = task.runtime.pipeline.current_stage
    if current_stage.stage == stage and current_stage.status in _TRUSTED_STAGE_MARKER_STATUSES:
        return True
    interruption = task.runtime.execution.interruption
    if interruption is not None and (interruption.resume_stage == stage or interruption.pipeline_status == stage):
        return True
    return False


def _normalize_stale_pipeline_statuses(
    state: WorkspaceState,
    tasks_by_id: dict[str, TaskRecord],
) -> list[TaskRecord]:
    active_stage = _live_active_pipeline_stage(state, tasks_by_id)
    mutated: list[TaskRecord] = []
    for task in tasks_by_id.values():
        if task.id == state.active_task_id:
            continue
        stage = str(task.pipeline_status)
        if stage in {"backlog", active_stage}:
            continue
        if task.status != TaskStatus.QUEUED:
            continue
        if task_has_resume_marker(task):
            continue
        now = utcnow()
        task.pipeline_status = PipelineStatus.BACKLOG
        task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now, stage="backlog")
        task.runtime.pipeline.updated_at = now
        if task.runtime.pipeline.last_outcome.kind == "interrupted":
            task.runtime.pipeline.last_outcome.stage = "backlog"
        mutated.append(task)
    return mutated


def set_active_task(root: Path, task_id: str | None) -> WorkspaceState:
    """Pin a specific task as the workspace's active task, bypassing the queue selector.

    Integration tests and helper fixtures use this to put a known task in the
    runner's slot directly; production code reaches active state through
    ``dequeue_next_task_selection`` instead.
    """
    with workspace_mutation_guard(root), workspace_lock(root):
        state = load_state(root)
        state.active_task_id = task_id
        if task_id is not None and task_id in state.queue:
            state.queue = [item for item in state.queue if item != task_id]
        validate_single_active_task(root, state)
        if task_id is None:
            save_state(root, state)
            return state
        task = require_task(root, task_id)
        if task.status == TaskStatus.QUEUED:
            task.status = TaskStatus.IN_PROGRESS
        persist_task_and_state(root, task=task, state=state)
        return state


def peek_next_task(root: Path) -> TaskRecord | None:
    """Return the next runnable task without dequeuing it; only test code calls this.

    Production status surfaces use ``peek_next_task_selection`` so they can
    surface blocked tasks too — this thin wrapper drops that information and
    is a candidate for inlining at the one test caller.
    """
    return peek_next_task_selection(root).task


def peek_next_task_selection(root: Path) -> TaskSelection:
    """Return the next runnable task plus blocked-task diagnostics, leaving the queue intact.

    Currently exercised only by queue-invariant tests; the public status
    surfaces use the dequeue path. If no production caller appears, fold it
    back into the dequeue helper.
    """
    recover_stale_runner_state(root)
    with workspace_mutation_guard(root), workspace_lock(root):
        state = load_state(root)
        validate_single_active_task(root, state)
        next_task, blocked, mutated, normalized_tasks = _resolve_next_task_from_state(root, state)
        if mutated:
            if normalized_tasks:
                persist_tasks_and_state(root, tasks=normalized_tasks, state=state)
            else:
                save_state(root, state)
        return TaskSelection(task=next_task, blocked=blocked)


def plan_task_selections(root: Path) -> TaskPlan:
    """Simulate dequeue iterations against a copy of state to preview the runner's plan.

    No callers — the ``litehive plan`` CLI surface this was written for never
    landed; safe to remove unless that surface is being revived.
    """
    recover_stale_runner_state(root)
    with workspace_mutation_guard(root), workspace_lock(root):
        state = load_state(root)
        validate_single_active_task(root, state)
        tasks_by_id = {task.id: task.model_copy(deep=True) for task in list_tasks(root, strict=False)}

        planned: list[TaskRecord] = []
        simulated_state = state.model_copy(deep=True)
        while True:
            next_task, blocked, _ = _resolve_next_task_from_snapshot(
                simulated_state,
                tasks_by_id,
            )
            if next_task is None:
                return TaskPlan(tasks=planned, blocked=blocked)

            planned.append(next_task.model_copy(deep=True))
            simulated_state.active_task_id = None
            simulated_state.queue = [item for item in simulated_state.queue if item != next_task.id]
            simulated_task = tasks_by_id[next_task.id]
            simulated_task.status = TaskStatus.DONE
            simulated_task.pipeline_status = PipelineStatus.DONE


def dequeue_next_task(root: Path) -> TaskRecord | None:
    """Pick the next runnable task and promote it to active; the runner's main entry point.

    The CLI runner loop and the one-shot ``litehive run`` command call this to
    advance the queue; callers that also need to render blocked-task reasons
    use ``dequeue_next_task_selection`` directly.
    """
    return dequeue_next_task_selection(root).task


def dequeue_next_task_selection(root: Path) -> TaskSelection:
    """Pick the next runnable task, promote it to active, and report blocked siblings.

    Drives the runner's task pickup: resolves the next eligible candidate,
    auto-recovers flagged tasks that still have budget, transitions the task
    from ``queued``/``interrupted`` to ``in_progress``, and persists the
    workspace mutation so the runner can begin executing.
    """
    recover_stale_runner_state(root)
    with workspace_mutation_guard(root), workspace_lock(root):
        state = load_state(root)
        original_queue = list(state.queue)
        validate_single_active_task(root, state)
        next_task, blocked, mutated, normalized_tasks = _resolve_next_task_from_state(root, state)
        if next_task is None:
            if mutated:
                if normalized_tasks:
                    persist_tasks_and_state(root, tasks=normalized_tasks, state=state)
                else:
                    save_state(root, state)
            return TaskSelection(task=None, blocked=blocked)
        if state.active_task_id != next_task.id:
            state.active_task_id = next_task.id
            state.queue = [item for item in state.queue if item != next_task.id]
            mutated = True
        if mutated:
            if next_task.status == TaskStatus.FLAGGED:
                if _needs_manual_intervention(next_task) or _is_recovery_budget_exhausted(next_task):
                    if state.active_task_id == next_task.id:
                        state.active_task_id = None
                    if mutated:
                        save_state(root, state)
                    return TaskSelection(task=None, blocked=blocked)
                recovery_stage = _auto_recovery_stage_for_flagged_task(next_task)
                record_recovery_report(
                    root,
                    next_task,
                    trigger_event_kind=TriggerEventKind.FLAGGED_TASK,
                    origin_stage=next_task.pipeline_status,
                    summary=(f"Recovered flagged task back to `{recovery_stage}` so it can run again."),
                    runnable_state="runnable",
                    failure_classification=next_task.runtime.pipeline.last_outcome.reason_code,
                    actions=[
                        RecoveryAction(
                            action="requeue_stage",
                            summary=f"Reset task from flagged to queued/{recovery_stage}.",
                            metadata={
                                "from_stage": next_task.pipeline_status,
                                "to_stage": recovery_stage,
                            },
                        )
                    ],
                )
                reset_task_for_recovery(
                    next_task,
                    status="queued",
                    pipeline_status=recovery_stage,
                    clear_last_outcome=False,
                )
                # Reset the current lifecycle cursor so the runner starts from
                # `ready` instead of re-emitting the sticky `failed` terminal
                # and looping forever. Transition/journal history remains the
                # source for prior-attempt metrics.
                from litehive.lifecycle.persistence import SqlitePersistence  # noqa: PLC0415

                SqlitePersistence(root).reset_current_lifecycle_state(next_task.id, preserve_run_memory=True)
            if next_task.status in {TaskStatus.QUEUED, TaskStatus.INTERRUPTED}:
                next_task.status = TaskStatus.IN_PROGRESS
            queue_additions = [task_id for task_id in state.queue if task_id not in original_queue]
            if normalized_tasks:
                tasks_to_persist = {task.id: task for task in normalized_tasks}
                tasks_to_persist[next_task.id] = next_task
                persist_tasks_and_state(
                    root,
                    tasks=list(tasks_to_persist.values()),
                    state=state,
                    protected_task_ids=queue_additions,
                )
            else:
                persist_task_and_state(
                    root,
                    task=next_task,
                    state=state,
                    protected_task_ids=queue_additions,
                )
        return TaskSelection(task=next_task, blocked=blocked)


def _is_parked_task(task: TaskRecord) -> bool:
    return task.status == TaskStatus.PARKED


def is_task_eligible_for_execution(task: TaskRecord) -> bool:
    """Decide whether a task is allowed to be dequeued or kept active right now.

    The single source of truth for "is this task runnable?" — the queue
    selector, workspace-repair flows, status diagnostics, and the
    single-active-task invariant in the workspace lock all consult this so
    they share the same view of terminal/blocked statuses.
    """
    if has_blocking_failed_run_history(task):
        return False
    if _has_terminal_execution_status(task):
        return False
    if _has_terminal_outcome_kind(task):
        return False
    if task.pipeline_status == PipelineStatus.DONE:
        return False
    if _needs_manual_intervention(task):
        return False
    if _is_recovery_budget_exhausted(task):
        return False
    if task.status in {TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.FLAGGED}:
        return True
    if task.status == TaskStatus.INTERRUPTED:
        return True
    return False


def _auto_recovery_stage_for_flagged_task(task: TaskRecord) -> str:
    if task.pipeline_status == PipelineStatus.COMMIT_TO_GIT:
        return TaskStage.COMMIT_TO_GIT.value
    return implementation_entry_stage(task)


def _is_task_completed(task: TaskRecord) -> bool:
    return task.status == TaskStatus.DONE and task.pipeline_status == PipelineStatus.DONE


def _task_blockers(task: TaskRecord, tasks_by_id: dict[str, TaskRecord]) -> list[str]:
    blockers: list[str] = []
    seen: set[str] = set()
    for dependency_id in task.depends_on:
        if dependency_id in seen:
            continue
        seen.add(dependency_id)
        dependency = tasks_by_id.get(dependency_id)
        if dependency is None:
            blockers.append(f"{dependency_id} (missing)")
            continue
        if not _is_task_completed(dependency):
            blockers.append(f"{dependency.id} ({dependency.status}/{dependency.pipeline_status})")
    return blockers


def validate_task_dependencies(root: Path, task_id: str, depends_on: list[str]) -> None:
    """Reject a task whose ``depends_on`` list is self-referential, missing, or cyclic.

    Called when persisting a new task and when ``litehive update`` rewrites a
    task's dependency list — refuses the mutation before it can corrupt the
    queue selector's blocked-task graph.
    """
    tasks_by_id = {task.id: task for task in list_tasks(root, strict=False)}
    seen: set[str] = set()
    for dependency_id in depends_on:
        if dependency_id in seen:
            continue
        seen.add(dependency_id)
        if dependency_id == task_id:
            raise ValueError(f"Task {task_id} cannot depend on itself")
        if dependency_id not in tasks_by_id:
            raise ValueError(f"Task {dependency_id} not found")
        if _dependency_reaches_task(task_id, dependency_id, tasks_by_id):
            raise ValueError(f"Task {task_id} dependency cycle detected via {dependency_id}")


def _dependency_reaches_task(task_id: str, dependency_id: str, tasks_by_id: dict[str, TaskRecord]) -> bool:
    stack = [dependency_id]
    seen: set[str] = set()
    while stack:
        current_id = stack.pop()
        if current_id == task_id:
            return True
        if current_id in seen:
            continue
        seen.add(current_id)
        current = tasks_by_id.get(current_id)
        if current is None:
            continue
        stack.extend(current.depends_on)
    return False


def _dependent_task_count(task_id: str, queue: list[str], tasks_by_id: dict[str, TaskRecord]) -> int:
    eligible_task_ids = {
        queued_id
        for queued_id in queue
        if ((queued_task := tasks_by_id.get(queued_id)) is not None and is_task_eligible_for_execution(queued_task))
    }
    reverse_dependencies: dict[str, set[str]] = {candidate_id: set() for candidate_id in eligible_task_ids}
    for queued_id in eligible_task_ids:
        queued_task = tasks_by_id[queued_id]
        for dependency_id in queued_task.depends_on:
            if dependency_id in reverse_dependencies:
                reverse_dependencies[dependency_id].add(queued_id)

    count = 0
    seen: set[str] = set()
    stack = list(reverse_dependencies.get(task_id, ()))
    while stack:
        dependent_id = stack.pop()
        if dependent_id in seen:
            continue
        seen.add(dependent_id)
        count += 1
        stack.extend(reverse_dependencies.get(dependent_id, ()))
    return count


def _is_interrupted_task(task: TaskRecord) -> bool:
    return is_task_eligible_for_execution(task) and (
        task.status == TaskStatus.IN_PROGRESS or task.pipeline_status != PipelineStatus.BACKLOG
    )


def _task_selection_key(
    task: TaskRecord,
    queue_index: int,
    queue: list[str],
    tasks_by_id: dict[str, TaskRecord],
) -> tuple[int | str, ...]:
    if _is_interrupted_task(task):
        interrupted_rank = 0
    else:
        interrupted_rank = 1
    return (
        queue_index,
        -_dependent_task_count(task.id, queue, tasks_by_id),
        interrupted_rank,
        task.id,
    )


def _resolve_next_task_from_state(
    root: Path, state: WorkspaceState
) -> tuple[TaskRecord | None, list[BlockedTask], bool, list[TaskRecord]]:

    tasks_by_id = {task.id: task for task in list_tasks(root, strict=False)}
    store = runtime_store(root)
    for queued_task_id in state.queue:
        if queued_task_id in tasks_by_id:
            continue
        if store.load_task_intent(queued_task_id) is not None:
            continue
        raise TaskLaunchFailure(
            context="pre_stage_setup_failed",
            summary=f"queued task {queued_task_id} is missing from SQLite task_intent",
            diagnostics={"task_id": queued_task_id, "storage": "sqlite"},
        )
    normalized_tasks = _normalize_stale_pipeline_statuses(state, tasks_by_id)
    next_task, blocked, snapshot_mutated = _resolve_next_task_from_snapshot(state, tasks_by_id)
    return next_task, blocked, snapshot_mutated or bool(normalized_tasks), normalized_tasks


def restore_missing_queued_tasks(
    state: WorkspaceState,
    tasks_by_id: dict[str, TaskRecord],
) -> list[str]:
    restored_front: list[str] = []
    queued_ids = set(state.queue)
    for task_id, task in tasks_by_id.items():
        if not is_task_eligible_for_execution(task):
            continue
        if task_id == state.active_task_id or task_id in queued_ids:
            continue
        # Missing resumable work must reclaim queue visibility ahead of later
        # queued tasks or the runner can hand off past unfinished execution.
        if task.status == TaskStatus.IN_PROGRESS or resumable_queue_stage(task) is not None:
            queued_ids.add(task_id)
            restored_front.append(task_id)
    if restored_front:
        state.queue = [*restored_front, *state.queue]
    return restored_front


def _resolve_next_task_from_snapshot(
    state: WorkspaceState,
    tasks_by_id: dict[str, TaskRecord],
) -> tuple[TaskRecord | None, list[BlockedTask], bool]:
    mutated = False
    blocked: list[BlockedTask] = []
    blocked_task_ids: set[str] = set()
    if restore_missing_queued_tasks(state, tasks_by_id):
        mutated = True
    if state.active_task_id is not None:
        active_task = tasks_by_id.get(state.active_task_id)
        if active_task is not None and is_task_eligible_for_execution(active_task):
            blockers = _task_blockers(active_task, tasks_by_id)
            if not blockers:
                return active_task, blocked, mutated
            if active_task.id not in state.queue:
                state.queue.insert(0, active_task.id)
            blocked.append(
                BlockedTask(
                    task_id=active_task.id,
                    title=active_task.title,
                    queue_position=1,
                    blocked_by=blockers,
                )
            )
            blocked_task_ids.add(active_task.id)
        state.active_task_id = None
        mutated = True

    ready_candidates: list[tuple[tuple[int, int, str], TaskRecord]] = []
    for index, next_id in enumerate(list(state.queue), start=1):
        next_task = tasks_by_id.get(next_id)
        if next_task is None or not is_task_eligible_for_execution(next_task):
            state.queue.remove(next_id)
            mutated = True
            continue
        blockers = _task_blockers(next_task, tasks_by_id)
        if blockers:
            if next_task.id not in blocked_task_ids:
                blocked.append(
                    BlockedTask(
                        task_id=next_task.id,
                        title=next_task.title,
                        queue_position=index,
                        blocked_by=blockers,
                    )
                )
                blocked_task_ids.add(next_task.id)
            continue
        ready_candidates.append(
            (
                _task_selection_key(
                    next_task,
                    queue_index=index,
                    queue=list(state.queue),
                    tasks_by_id=tasks_by_id,
                ),
                next_task,
            )
        )

    if ready_candidates:
        ready_candidates.sort(key=lambda item: item[0])
        return ready_candidates[0][1], blocked, mutated

    return None, blocked, mutated


def clear_active_task(root: Path) -> WorkspaceState:
    """Detach whichever task currently sits in the active slot.

    No callers — operators clear the active task via stop/close/abandon flows
    that already null out ``state.active_task_id`` themselves. Candidate for
    removal.
    """
    return set_active_task(root, None)


def restore_untouched_active_task(root: Path) -> WorkspaceState:
    """Push the active task back onto the queue if it was never actually started.

    No callers — duplicates the resume/recovery logic in
    ``recovery.execution_recovery`` and ``restore_missing_queued_tasks``. Looks
    like a leftover from before workspace-repair owned this concern.
    """
    with workspace_mutation_guard(root), workspace_lock(root):
        state = load_state(root)
        validate_single_active_task(root, state)
        if state.active_task_id is None:
            return state

        task = get_task(root, state.active_task_id)
        if task is not None and _should_requeue_commit_stage_task(task):
            prepare_interrupted_task(
                root,
                task,
                stage=TaskStage.COMMIT_TO_GIT.value,
                summary="Interrupted `commit_to_git` run recovered. Resume from `commit_to_git`.",
                reason=stale_interruption_reason(task, TaskStage.COMMIT_TO_GIT.value),
            )
            task.status = TaskStatus.QUEUED
            enqueue_recovered_task(state, task.id)
            state.active_task_id = None
            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message=interruption_journal_message(task),
            )
            return state

        if (
            task is not None
            and is_task_eligible_for_execution(task)
            and task.runtime.pipeline.execution_status != "running"
        ):
            task.status = TaskStatus.QUEUED
            enqueue_recovered_task(state, task.id)
            state.active_task_id = None
            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message=f"Restored untouched active task to queue at `{task.pipeline_status}`.",
            )
            return state

        if task is not None and is_task_eligible_for_execution(task):
            prepare_interrupted_task(
                root,
                task,
                stage=task.pipeline_status,
                summary=f"Interrupted run recovered. Resume from `{task.pipeline_status}`.",
                reason=stale_interruption_reason(task, task.pipeline_status),
            )
            if not _is_parked_task(task):
                task.status = TaskStatus.QUEUED
                enqueue_recovered_task(state, task.id)
            state.active_task_id = None
            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message=interruption_journal_message(task),
            )
            return state

        state.active_task_id = None
        save_state(root, state)
        return state


def active_task_markers(root: Path, state: WorkspaceState | None = None) -> dict[str, list[str]]:
    """Collect every signal that says "this task is active", keyed by task id.

    Underpins the single-active-task invariant: the workspace lock and the
    task-stop flow call this to prove no two tasks claim ``in_progress`` /
    ``running`` at the same time, and to spell out which signal disagrees when
    they do.
    """
    markers: dict[str, list[str]] = {}
    current_state = state or load_state(root)
    tasks = list_tasks(root, strict=False)
    tasks_by_id = {task.id: task for task in tasks}
    if current_state.active_task_id is None:
        active_task = None
    else:
        active_task = tasks_by_id.get(current_state.active_task_id)
    if active_task is not None and (
        is_task_eligible_for_execution(active_task) or active_task.runtime.pipeline.execution_status == "running"
    ):
        markers.setdefault(current_state.active_task_id, []).append("workspace.active_task_id")
    for task in tasks:
        if (
            task.status == TaskStatus.IN_PROGRESS
            and task.pipeline_status != PipelineStatus.DONE
            and is_task_eligible_for_execution(task)
        ):
            markers.setdefault(task.id, []).append("task.status=in_progress")
        if task.runtime.pipeline.execution_status == "running":
            markers.setdefault(task.id, []).append("runtime.pipeline.execution_status=running")
    return markers


def validate_single_active_task(root: Path, state: WorkspaceState | None = None) -> None:
    """Raise ``WorkspaceConflictError`` if more than one task is currently active.

    Hot-path guard: every queue mutation, runner pickup, and stop flow calls
    this before touching state so a corrupted workspace fails loudly instead
    of silently launching two runners against the same worktree.
    """
    markers = active_task_markers(root, state)
    if len(markers) <= 1:
        return
    details = "; ".join(f"{task_id} ({', '.join(task_markers)})" for task_id, task_markers in sorted(markers.items()))
    raise WorkspaceConflictError(
        f"workspace has multiple active tasks: {details}. Clear the stale active task state before running again."
    )
