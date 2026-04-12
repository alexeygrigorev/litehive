"""Queue operations: selection, blocking, dependency validation."""

import logging
from pathlib import Path

from litehive.config import VALID_POOL_SELECTION_POLICIES, load_config
from litehive.models import RecoveryAction, TaskRecord, WorkspaceState

from .constants import TASK_PRIORITY_ORDER
from .models import BlockedTask, TaskPlan, TaskSelection, WorkspaceConflictError

logger = logging.getLogger(__name__)


def _is_hook_reject_loop_flagged(task: TaskRecord) -> bool:
    return task.status == "flagged" and task.flag_reason == "hook_reject_loop"


def set_active_task(root: Path, task_id: str | None) -> WorkspaceState:
    from .crud import require_task
    from litehive.workspace.locking import workspace_lock, workspace_mutation_guard
    from .persistence import load_state, save_state
    from litehive.workspace.workflow import persist_task_and_state

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
        if task.status == "queued":
            task.status = "in_progress"
        persist_task_and_state(root, task=task, state=state)
        return state


def peek_next_task(root: Path) -> TaskRecord | None:
    return peek_next_task_selection(root).task


def peek_next_task_selection(root: Path) -> TaskSelection:
    from litehive.workspace.locking import workspace_lock, workspace_mutation_guard
    from .persistence import load_state, save_state
    from litehive.recovery import recover_stale_runner_state

    recover_stale_runner_state(root)
    with workspace_mutation_guard(root), workspace_lock(root):
        state = load_state(root)
        validate_single_active_task(root, state)
        next_task, blocked, mutated = _resolve_next_task_from_state(root, state)
        if mutated:
            save_state(root, state)
        return TaskSelection(task=next_task, blocked=blocked)


def plan_task_selections(root: Path) -> TaskPlan:
    from .crud import list_tasks
    from litehive.workspace.locking import workspace_lock, workspace_mutation_guard
    from .persistence import load_state
    from litehive.recovery import recover_stale_runner_state

    recover_stale_runner_state(root)
    with workspace_mutation_guard(root), workspace_lock(root):
        state = load_state(root)
        validate_single_active_task(root, state)
        tasks_by_id = {task.id: task.model_copy(deep=True) for task in list_tasks(root)}
        policy = load_config(root).pool_selection_policy
        if policy not in VALID_POOL_SELECTION_POLICIES:
            policy = "dependency_aware"

        planned: list[TaskRecord] = []
        simulated_state = state.model_copy(deep=True)
        while True:
            next_task, blocked, _ = _resolve_next_task_from_snapshot(
                simulated_state,
                tasks_by_id,
                policy=policy,
            )
            if next_task is None:
                return TaskPlan(tasks=planned, blocked=blocked)

            planned.append(next_task.model_copy(deep=True))
            simulated_state.active_task_id = None
            simulated_state.queue = [item for item in simulated_state.queue if item != next_task.id]
            simulated_task = tasks_by_id[next_task.id]
            simulated_task.status = "done"
            simulated_task.pipeline_status = "done"


def dequeue_next_task(root: Path) -> TaskRecord | None:
    return dequeue_next_task_selection(root).task


def dequeue_next_task_selection(root: Path) -> TaskSelection:
    from litehive.workspace.locking import workspace_lock, workspace_mutation_guard
    from .persistence import load_state, save_state
    from .queue_management import reset_task_for_recovery
    from litehive.recovery import recover_stale_runner_state
    from .reports import record_recovery_report
    from litehive.workspace.workflow import persist_task_and_state

    recover_stale_runner_state(root)
    with workspace_mutation_guard(root), workspace_lock(root):
        state = load_state(root)
        validate_single_active_task(root, state)
        next_task, blocked, mutated = _resolve_next_task_from_state(root, state)
        if next_task is None:
            if mutated:
                save_state(root, state)
            return TaskSelection(task=None, blocked=blocked)
        if state.active_task_id != next_task.id:
            state.active_task_id = next_task.id
            state.queue = [item for item in state.queue if item != next_task.id]
            mutated = True
        if mutated:
            if next_task.status == "flagged":
                if _is_hook_reject_loop_flagged(next_task):
                    if state.active_task_id == next_task.id:
                        state.active_task_id = None
                    if mutated:
                        save_state(root, state)
                    return TaskSelection(task=None, blocked=blocked)
                recovery_stage = _auto_recovery_stage_for_flagged_task(next_task)
                record_recovery_report(
                    root,
                    next_task,
                    trigger="flagged_task",
                    stage=next_task.pipeline_status,
                    summary=(
                        f"Recovered flagged task back to `{recovery_stage}` so it can run again."
                    ),
                    runnable_state="runnable",
                    failure_classification=next_task.runtime.last_outcome.reason_code,
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
            if next_task.status in {"queued", "interrupted"}:
                next_task.status = "in_progress"
            persist_task_and_state(root, task=next_task, state=state)
        return TaskSelection(task=next_task, blocked=blocked)


def _is_parked_task(task: TaskRecord) -> bool:
    return task.status == "parked"


def is_task_eligible_for_execution(task: TaskRecord) -> bool:
    if task.pipeline_status == "done":
        return False
    if _is_hook_reject_loop_flagged(task):
        return False
    if task.status in {"queued", "in_progress", "flagged"}:
        return True
    if task.status == "interrupted":
        return True
    return False


def _auto_recovery_stage_for_flagged_task(task: TaskRecord) -> str:
    from .normalization import implementation_entry_stage

    if task.pipeline_status == "commit_to_git":
        return "commit_to_git"
    return implementation_entry_stage(task)


def _is_task_completed(task: TaskRecord) -> bool:
    return task.status == "done" and task.pipeline_status == "done"


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


def validate_task_dependencies(root: Path, *, task_id: str, depends_on: list[str]) -> None:
    from .crud import list_tasks

    tasks_by_id = {task.id: task for task in list_tasks(root)}
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


def _dependency_reaches_task(
    task_id: str, dependency_id: str, tasks_by_id: dict[str, TaskRecord]
) -> bool:
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


def _dependent_task_count(
    task_id: str, queue: list[str], tasks_by_id: dict[str, TaskRecord]
) -> int:
    eligible_task_ids = {
        queued_id
        for queued_id in queue
        if (
            (queued_task := tasks_by_id.get(queued_id)) is not None
            and is_task_eligible_for_execution(queued_task)
        )
    }
    reverse_dependencies: dict[str, set[str]] = {
        candidate_id: set() for candidate_id in eligible_task_ids
    }
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
        task.status == "in_progress" or task.pipeline_status != "backlog"
    )


def _task_selection_key(
    task: TaskRecord,
    *,
    queue_index: int,
    queue: list[str],
    tasks_by_id: dict[str, TaskRecord],
    policy: str,
) -> tuple[int | str, ...]:
    interrupted_rank = 0 if _is_interrupted_task(task) else 1
    if policy == "fifo":
        return (interrupted_rank, queue_index, task.id)
    if policy == "priority_first":
        return (TASK_PRIORITY_ORDER.get(task.priority, 2), queue_index, interrupted_rank, task.id)
    if policy == "dependency_aware":
        return (
            queue_index,
            -_dependent_task_count(task.id, queue, tasks_by_id),
            interrupted_rank,
            task.id,
        )
    raise ValueError(f"Unsupported pool selection policy '{policy}'")


def _resolve_next_task_from_state(
    root: Path, state: WorkspaceState
) -> tuple[TaskRecord | None, list[BlockedTask], bool]:
    from .crud import list_tasks

    tasks_by_id = {task.id: task for task in list_tasks(root)}
    policy = load_config(root).pool_selection_policy
    if policy not in VALID_POOL_SELECTION_POLICIES:
        policy = "dependency_aware"
    next_task, blocked, snapshot_mutated = _resolve_next_task_from_snapshot(
        state, tasks_by_id, policy=policy
    )
    return next_task, blocked, snapshot_mutated


def restore_missing_queued_tasks(
    state: WorkspaceState,
    tasks_by_id: dict[str, TaskRecord],
) -> list[str]:
    restored: list[str] = []
    queued_ids = set(state.queue)
    for task_id, task in tasks_by_id.items():
        if task.status not in {"queued", "interrupted", "flagged"}:
            continue
        if _is_hook_reject_loop_flagged(task):
            continue
        if task.pipeline_status == "done":
            continue
        if _is_parked_task(task):
            continue
        if task_id == state.active_task_id or task_id in queued_ids:
            continue
        state.queue.append(task_id)
        queued_ids.add(task_id)
        restored.append(task_id)
    return restored




def _resolve_next_task_from_snapshot(
    state: WorkspaceState,
    tasks_by_id: dict[str, TaskRecord],
    *,
    policy: str,
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
                    policy=policy,
                ),
                next_task,
            )
        )

    if ready_candidates:
        ready_candidates.sort(key=lambda item: item[0])
        return ready_candidates[0][1], blocked, mutated

    return None, blocked, mutated


def clear_active_task(root: Path) -> WorkspaceState:
    return set_active_task(root, None)


def restore_untouched_active_task(root: Path) -> WorkspaceState:
    from .crud import get_task
    from litehive.workspace.locking import workspace_lock, workspace_mutation_guard
    from .persistence import load_state, save_state
    from .queue_management import enqueue_recovered_task
    from litehive.recovery import (
        prepare_interrupted_task,
        should_requeue_commit_stage_task,
        stale_interruption_reason,
        interruption_journal_message,
    )
    from litehive.workspace.workflow import persist_task_and_state

    with workspace_mutation_guard(root), workspace_lock(root):
        state = load_state(root)
        validate_single_active_task(root, state)
        if state.active_task_id is None:
            return state

        task = get_task(root, state.active_task_id)
        if task is not None and should_requeue_commit_stage_task(task):
            prepare_interrupted_task(
                root,
                task,
                stage="commit_to_git",
                summary="Interrupted `commit_to_git` run recovered. Resume from `commit_to_git`.",
                reason=stale_interruption_reason(task, "commit_to_git"),
            )
            task.status = "queued"
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
            and task.runtime.execution_status != "running"
        ):
            task.status = "queued"
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
                task.status = "queued"
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
    from .crud import list_tasks
    from .persistence import load_state

    markers: dict[str, list[str]] = {}
    current_state = state or load_state(root)
    if current_state.active_task_id is not None:
        markers.setdefault(current_state.active_task_id, []).append("workspace.active_task_id")
    for task in list_tasks(root):
        if task.runtime.execution_status == "running":
            markers.setdefault(task.id, []).append("runtime.execution_status=running")
    return markers


def validate_single_active_task(root: Path, state: WorkspaceState | None = None) -> None:
    markers = active_task_markers(root, state)
    if len(markers) <= 1:
        return
    details = "; ".join(
        f"{task_id} ({', '.join(task_markers)})"
        for task_id, task_markers in sorted(markers.items())
    )
    raise WorkspaceConflictError(
        f"workspace has multiple active tasks: {details}. Clear the stale active task state before running again."
    )
