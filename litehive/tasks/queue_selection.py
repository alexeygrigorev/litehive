"""Queue read/selection: dequeue, peek, active-task pinning, and the
single-active-task invariant.

These functions intersect with both the ``runtime_store`` (SQLite-backed
intent rows) and the workspace JSON state, so they own the orchestration that
``queue_eligibility`` (pure predicates) and ``queue_mutations`` (list ops)
must not reach into.
"""

import logging
from pathlib import Path

from litehive.domain.common import OutcomeKind, PipelineStatus, TaskExecutionStatus, TaskStage, TaskStatus, utcnow
from litehive.domain.reports import RecoveryAction
from litehive.domain.recovery import TriggerEventKind
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.domain.task_ops import BlockedTask, TaskSelection, WorkspaceConflictError
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.recovery.detection import TaskLaunchFailure
from litehive.recovery.execution_recovery import (
    interruption_journal_message,
    prepare_interrupted_task,
    recover_stale_runner_state_for_workspace,
    stale_interruption_reason,
)
from litehive.state.locking import (
    workspace_lock,
    workspace_mutation_guard_for_workspace,
)
from litehive.state.persist import (
    load_state,
    persist_task_and_state,
    persist_tasks_and_state,
    save_state,
)
from litehive.state.records import (
    get_task,
    list_tasks,
    require_task,
)
from litehive.state.store import runtime_store
from litehive.tasks.queue_eligibility import (
    _auto_recovery_stage_for_flagged_task,
    _is_parked_task,
    _is_interrupted_task,
    _is_recovery_budget_exhausted,
    _live_active_pipeline_stage,
    _needs_manual_intervention,
    _should_requeue_commit_stage_task,
    _task_blockers,
    is_task_eligible_for_execution,
    resumable_queue_stage,
    task_has_resume_marker,
)
from litehive.tasks.queue_mutations import (
    enqueue_recovered_task,
    reset_task_for_recovery,
)
from litehive.tasks.recovery_reports import record_recovery_report
from litehive.tasks.runtime import idle_stage_state
from litehive.workspace import Workspace

logger = logging.getLogger(__name__)


def _normalize_stale_pipeline_statuses(
    state: WorkspaceState,
    tasks_by_id: dict[str, TaskRecord],
) -> list[TaskRecord]:
    """
    Reset queued non-active tasks back to ``backlog`` when their stage is stale.

    Called by ``_resolve_next_task_from_state`` before queue selection: a
    queued task that still claims an in-flight stage from a prior run would
    be picked up at that stage and skip the proper kickoff. Returns the
    mutated tasks so the caller can persist them atomically.
    """
    active_stage = _live_active_pipeline_stage(state, tasks_by_id)
    mutated: list[TaskRecord] = []
    for task in tasks_by_id.values():
        if task.id == state.active_task_id:
            continue
        if task.pipeline_status == PipelineStatus.BACKLOG:
            continue
        if active_stage is not None and task.pipeline_status == active_stage:
            continue
        if task.status != TaskStatus.QUEUED:
            continue
        if task_has_resume_marker(task):
            continue
        now = utcnow()
        task.pipeline_status = PipelineStatus.BACKLOG
        task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now, stage="backlog")
        task.runtime.pipeline.updated_at = now
        last_outcome = task.runtime.pipeline.last_outcome
        if last_outcome.kind == OutcomeKind.INTERRUPTED:
            task.runtime.pipeline.last_outcome = last_outcome.model_copy(update={"stage": "backlog"})
        mutated.append(task)
    return mutated


def set_active_task(workspace: Workspace, task_id: str | None) -> WorkspaceState:
    """
    Pin a specific task as the workspace's active task, bypassing selection.

    Integration tests and helper fixtures use this to put a known task in
    the runner's slot directly; production code reaches active state through
    ``dequeue_next_task_selection`` so the eligibility checks and audit
    bookkeeping run.
    """
    with workspace_mutation_guard_for_workspace(workspace), workspace_lock(workspace.root):
        state = load_state(workspace.root)
        state.active_task_id = task_id
        if task_id is not None and task_id in state.queue:
            state.queue = [item for item in state.queue if item != task_id]
        validate_single_active_task(workspace.root, state)
        if task_id is None:
            save_state(workspace.root, state)
            return state
        task = require_task(workspace.root, task_id)
        if task.status == TaskStatus.QUEUED:
            task.status = TaskStatus.IN_PROGRESS
        persist_task_and_state(workspace.root, task=task, state=state)
        return state


def peek_next_task(workspace: Workspace) -> TaskRecord | None:
    """
    Return the next runnable task without dequeuing it.

    Only test code calls this; production status surfaces use
    ``peek_next_task_selection`` so they can also report the blocked tasks.
    This thin wrapper drops that information and is a candidate for
    inlining at its one test caller.
    """
    return peek_next_task_selection(workspace).task


def peek_next_task_selection(workspace: Workspace) -> TaskSelection:
    """
    Return the next runnable task plus blocked-task diagnostics, queue intact.

    Currently exercised only by queue-invariant tests; the public status
    surfaces use the dequeue path instead. If no production caller appears,
    fold it back into the dequeue helper rather than carrying two near-copies.
    """
    recover_stale_runner_state_for_workspace(workspace)
    with workspace_mutation_guard_for_workspace(workspace), workspace_lock(workspace.root):
        state = load_state(workspace.root)
        validate_single_active_task(workspace.root, state)
        next_task, blocked, mutated, normalized_tasks = _resolve_next_task_from_state(workspace.root, state)
        if mutated:
            if normalized_tasks:
                persist_tasks_and_state(workspace.root, tasks=normalized_tasks, state=state)
            else:
                save_state(workspace.root, state)
        return TaskSelection(task=next_task, blocked=blocked)


def dequeue_next_task(workspace: Workspace) -> TaskRecord | None:
    """
    Pick the next runnable task and promote it to active.

    The runner's main entry point: the CLI runner loop and the one-shot
    ``litehive run`` command call this to advance the queue. Callers that
    also need blocked-task reasons use ``dequeue_next_task_selection``
    directly so they don't have to re-walk the queue to recover them.
    """
    return dequeue_next_task_selection(workspace).task


def dequeue_next_task_selection(workspace: Workspace) -> TaskSelection:
    """
    Pick the next runnable task, promote it to active, and report blocked siblings.

    The runner's pickup driver: resolves the next eligible candidate,
    auto-recovers flagged tasks that still have budget, transitions the
    chosen task from ``queued``/``interrupted`` to ``in_progress``, and
    persists the workspace mutation so the runner can begin executing
    without a second round-trip.
    """
    recover_stale_runner_state_for_workspace(workspace)
    with workspace_mutation_guard_for_workspace(workspace), workspace_lock(workspace.root):
        state = load_state(workspace.root)
        original_queue = list(state.queue)
        validate_single_active_task(workspace.root, state)
        next_task, blocked, mutated, normalized_tasks = _resolve_next_task_from_state(workspace.root, state)
        if next_task is None:
            if mutated:
                if normalized_tasks:
                    persist_tasks_and_state(workspace.root, tasks=normalized_tasks, state=state)
                else:
                    save_state(workspace.root, state)
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
                        save_state(workspace.root, state)
                    return TaskSelection(task=None, blocked=blocked)
                recovery_stage = _auto_recovery_stage_for_flagged_task(next_task)
                record_recovery_report(
                    workspace,
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
                    status=TaskStatus.QUEUED,
                    pipeline_status=recovery_stage,
                    clear_last_outcome=False,
                )
                # Reset the current lifecycle cursor so the runner starts from
                # `ready` instead of re-emitting the sticky `failed` terminal
                # and looping forever. Transition/journal history remains the
                # source for prior-attempt metrics.
                SqlitePersistence(workspace).reset_current_lifecycle_state(next_task.id, preserve_run_memory=True)
            if next_task.status in {TaskStatus.QUEUED, TaskStatus.INTERRUPTED}:
                next_task.status = TaskStatus.IN_PROGRESS
            queue_additions = [task_id for task_id in state.queue if task_id not in original_queue]
            if normalized_tasks:
                tasks_to_persist = {task.id: task for task in normalized_tasks}
                tasks_to_persist[next_task.id] = next_task
                persist_tasks_and_state(
                    workspace.root,
                    tasks=list(tasks_to_persist.values()),
                    state=state,
                    protected_task_ids=queue_additions,
                )
            else:
                persist_task_and_state(
                    workspace.root,
                    task=next_task,
                    state=state,
                    protected_task_ids=queue_additions,
                )
        return TaskSelection(task=next_task, blocked=blocked)


def _dependent_task_count(task_id: str, queue: list[str], tasks_by_id: dict[str, TaskRecord]) -> int:
    """
    Count how many other queued tasks transitively depend on ``task_id``.

    Feeds the selection key in ``_task_selection_key``: tasks that unblock
    more downstream work win ties so the queue drains breadth-first instead
    of starving siblings behind a popular dependency.
    """
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


def _task_selection_key(
    task: TaskRecord,
    queue_index: int,
    queue: list[str],
    tasks_by_id: dict[str, TaskRecord],
) -> tuple[int | str, ...]:
    """
    Build the sort key the selector uses to break ties between ready candidates.

    Called once per ready task in ``_resolve_next_task_from_snapshot``:
    prefers earlier queue position, then more-blocked-on tasks, then
    interrupted tasks over fresh ones, then stable-by-id. The tuple
    ordering is the runner's policy — change it here, not at the call site.
    """
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
    """
    Load tasks from disk, normalise stale stages, resolve the next runnable task.

    The disk-touching wrapper around ``_resolve_next_task_from_snapshot``
    shared by peek/dequeue/plan: also enforces that every queued task id
    has a matching SQLite intent row, raising ``TaskLaunchFailure`` so the
    runner surfaces missing-intent corruption instead of silently dropping
    the task.
    """
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
    """
    Push resumable tasks that fell off the queue back to its front.

    Called from ``_resolve_next_task_from_snapshot`` before candidate
    scoring: when a task is mid-execution or carries a resume marker but is
    no longer in ``state.queue``, it must be reinstated at the head or the
    runner will pick a fresh task and leave unfinished work stranded.
    """
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
    """
    Pure-snapshot next-task resolution honouring active, blocked, and tie-break order.

    The shared core for peek/dequeue: walks an in-memory ``state`` plus
    ``tasks_by_id`` and returns ``(chosen_task, blocked, mutated)``.
    """
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

    ready_candidates: list[tuple[tuple[int | str, ...], TaskRecord]] = []
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


def clear_active_task(workspace: Workspace) -> WorkspaceState:
    """
    Detach whichever task currently sits in the active slot.

    No callers — operators clear the active task via stop/close/abandon
    flows that already null out ``state.active_task_id`` themselves.
    Candidate for removal.
    """
    return set_active_task(workspace, None)


def restore_untouched_active_task(workspace: Workspace) -> WorkspaceState:
    """
    Push the active task back onto the queue if it was never actually started.

    No callers — duplicates the resume/recovery logic in
    ``recovery.execution_recovery`` and ``restore_missing_queued_tasks``;
    looks like a leftover from before workspace-repair owned this concern.
    """
    root = workspace.root
    with workspace_mutation_guard_for_workspace(workspace), workspace_lock(root):
        state = load_state(root)
        validate_single_active_task(root, state)
        if state.active_task_id is None:
            return state

        task = get_task(root, state.active_task_id)
        if task is not None and _should_requeue_commit_stage_task(task):
            prepare_interrupted_task(
                workspace,
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
            and task.runtime.pipeline.execution_status != TaskExecutionStatus.RUNNING
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
                workspace,
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
    """
    Collect every signal that says "this task is active", keyed by task id.

    Underpins the single-active-task invariant: the workspace lock and the
    task-stop flow call this to prove no two tasks claim ``in_progress``
    or ``running`` at once, and to spell out which signal disagrees when
    they do (so the conflict message names the failing slot).
    """
    markers: dict[str, list[str]] = {}
    current_state = state or load_state(root)
    tasks = list_tasks(root, strict=False)
    tasks_by_id = {task.id: task for task in tasks}
    if current_state.active_task_id is None:
        active_task = None
    else:
        active_task = tasks_by_id.get(current_state.active_task_id)
    if (
        active_task is not None
        and current_state.active_task_id is not None
        and (
            is_task_eligible_for_execution(active_task)
            or active_task.runtime.pipeline.execution_status == TaskExecutionStatus.RUNNING
        )
    ):
        markers.setdefault(current_state.active_task_id, []).append("workspace.active_task_id")
    for task in tasks:
        if (
            task.status == TaskStatus.IN_PROGRESS
            and task.pipeline_status != PipelineStatus.DONE
            and is_task_eligible_for_execution(task)
        ):
            markers.setdefault(task.id, []).append("task.status=in_progress")
        if task.runtime.pipeline.execution_status == TaskExecutionStatus.RUNNING:
            markers.setdefault(task.id, []).append("runtime.pipeline.execution_status=running")
    return markers


def validate_single_active_task(root: Path, state: WorkspaceState | None = None) -> None:
    """
    Raise ``WorkspaceConflictError`` if more than one task is currently active.

    Hot-path guard: every queue mutation, runner pickup, and stop flow
    calls this before touching state so a corrupted workspace fails loudly
    instead of silently launching two runners against the same worktree.
    """
    markers = active_task_markers(root, state)
    if len(markers) <= 1:
        return
    details = _format_active_task_markers(markers)
    raise WorkspaceConflictError(
        f"workspace has multiple active tasks: {details}. Clear the stale active task state before running again."
    )


def _format_active_task_markers(markers: dict[str, list[str]]) -> str:
    """
    Render the per-task marker list for the conflict error message.

    Each task is formatted as ``T-NNNN (marker1, marker2)`` and
    joined by ``;`` so the operator can see every task plus the
    flags that mark it active. Sorting on task id keeps the
    message stable across runs. Caller:
    :func:`validate_single_active_task`.
    """
    fragments: list[str] = []
    for task_id, task_markers in sorted(markers.items()):
        joined_markers = ", ".join(task_markers)
        fragments.append(f"{task_id} ({joined_markers})")
    return "; ".join(fragments)
