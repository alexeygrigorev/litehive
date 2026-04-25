"""Queue operations: list management (enqueue/move/prioritize) and selection
logic (dequeue, block, dep-resolve)."""

import logging
from pathlib import Path

from litehive.domain.common import utcnow
from litehive.domain.reports import RecoveryAction
from litehive.domain.recovery import TriggerEventKind
from litehive.domain.runtime import TaskOutcomeState
from litehive.domain.task import TaskRecord, WorkspaceState

from litehive.state.records import set_task_commit_sha
from litehive.domain.task_ops import BlockedTask, TaskPlan, TaskSelection, WorkspaceConflictError
from litehive.state.persist import load_state, save_state_without_runner_guard
from litehive.state.locking import ensure_future_task_mutation_allowed, workspace_lock
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.failed_runs import has_blocking_failed_run_history
from litehive.tasks.runtime import clear_task_run_activity, idle_stage_state

logger = logging.getLogger(__name__)

_TERMINAL_EXECUTION_STATUSES = {"done", "cancelled", "failed", "blocked", "interrupted"}
_TERMINAL_OUTCOME_KINDS = {"duplicate", "deferred", "wont_do"}
_TRUSTED_IDLE_STAGE_STATUSES = {"idle", "paused", "interrupted"}
_RESUMABLE_PIPELINE_STAGES = {"grooming", "implementing", "testing", "accepting", "commit_to_git"}


# --- list ops ---


def enqueue_task(root: Path, task_id: str) -> WorkspaceState:
    return _enqueue_task(root, task_id, front=False)


def enqueue_task_front(root: Path, task_id: str) -> WorkspaceState:
    return _enqueue_task(root, task_id, front=True)


def _enqueue_task(root: Path, task_id: str, *, front: bool) -> WorkspaceState:
    from litehive.state.records import require_task

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
    from litehive.state.records import require_task

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
    from litehive.state.records import require_task

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
    *,
    status: str,
    pipeline_status: str,
    clear_last_outcome: bool = True,
    preserve_continuation_handoff: bool = False,
) -> None:
    now = utcnow()
    task.status = status
    task.pipeline_status = pipeline_status
    clear_task_run_activity(task, execution_status="idle", updated_at=now, clear_interruption=True)
    task.runtime.retry_count = 0
    task.runtime.retry_limit = 0
    if not preserve_continuation_handoff:
        task.runtime.continuation_handoff = None
    task.runtime.current_stage = idle_stage_state(updated_at=now, stage=pipeline_status)
    if clear_last_outcome:
        task.runtime.last_outcome = TaskOutcomeState()
    elif task.runtime.last_outcome.kind == "interrupted":
        task.runtime.last_outcome.stage = pipeline_status


def enqueue_recovered_task(state: WorkspaceState, task_id: str) -> None:
    state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
    state.queue.append(task_id)


def prepare_completed_task_for_recovery(task: TaskRecord, *, recovery_stage: str) -> None:
    reset_task_for_recovery(
        task,
        status="queued",
        pipeline_status=recovery_stage,
    )
    set_task_commit_sha(task, None)


# --- selection ---


def _normalize_resumable_stage_name(stage: str | None) -> str | None:
    if stage == "merge_failed":
        return "commit_to_git"
    if stage in _RESUMABLE_PIPELINE_STAGES:
        return stage
    return None


def resumable_queue_stage(task: TaskRecord) -> str | None:
    interruption = task.runtime.interruption
    handoff = task.runtime.continuation_handoff
    current_stage = task.runtime.current_stage
    candidates = [
        task.pipeline_status,
        None if interruption is None else interruption.resume_stage,
        None if interruption is None else interruption.pipeline_status,
        None if handoff is None else handoff.stage,
    ]
    if current_stage.status in _TRUSTED_IDLE_STAGE_STATUSES:
        candidates.append(current_stage.stage)
    for candidate in candidates:
        normalized = _normalize_resumable_stage_name(None if candidate is None else str(candidate))
        if normalized is not None:
            return normalized
    return None


def canonicalize_resumable_queue_task(task: TaskRecord, *, stage: str | None = None) -> str | None:
    target_stage = _normalize_resumable_stage_name(stage) if stage is not None else resumable_queue_stage(task)
    if target_stage is None:
        return None
    now = clear_task_run_activity(task, execution_status="idle")
    task.status = "queued"
    task.pipeline_status = target_stage
    task.runtime.current_stage = idle_stage_state(updated_at=now, stage=target_stage)
    if task.runtime.last_outcome.kind == "interrupted":
        task.runtime.last_outcome.stage = target_stage
    return target_stage


def _needs_manual_intervention(task: TaskRecord) -> bool:
    return has_blocking_failed_run_history(task) or (
        task.status == "flagged"
        and task.flag_reason in {
            "hook_reject_loop",
            "rejection_loop_detected",
            "semantic_reject",
        }
    )


def _is_recovery_budget_exhausted(task: TaskRecord) -> bool:
    return task.status == "flagged" and task.flag_reason in {
        "crash_budget_exhausted",
        "recovery_budget_exhausted",
        "recovery_failed",
    }


def _should_requeue_commit_stage_task(task: TaskRecord) -> bool:
    return task.pipeline_status == "commit_to_git" and task.status in {
        "queued",
        "in_progress",
        "interrupted",
    }


def _has_terminal_execution_status(task: TaskRecord) -> bool:
    return str(task.runtime.execution_status) in _TERMINAL_EXECUTION_STATUSES


def _has_terminal_outcome_kind(task: TaskRecord) -> bool:
    kind = task.runtime.last_outcome.kind
    return kind is not None and str(kind) in _TERMINAL_OUTCOME_KINDS


def _live_active_pipeline_stage(state: WorkspaceState, tasks_by_id: dict[str, TaskRecord]) -> str | None:
    if state.active_task_id is None:
        return None
    active_task = tasks_by_id.get(state.active_task_id)
    if active_task is None:
        return None
    current_stage = active_task.runtime.current_stage
    if str(active_task.runtime.execution_status) == "running" or current_stage.status == "running":
        return str(current_stage.stage or active_task.pipeline_status)
    return None


def task_has_resume_marker(task: TaskRecord) -> bool:
    stage = str(task.pipeline_status)
    current_stage = task.runtime.current_stage
    if current_stage.stage == stage and current_stage.status in _TRUSTED_IDLE_STAGE_STATUSES:
        return True
    interruption = task.runtime.interruption
    if interruption is not None and (interruption.resume_stage == stage or interruption.pipeline_status == stage):
        return True
    handoff = task.runtime.continuation_handoff
    if handoff is not None and handoff.stage == stage:
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
        if task.status != "queued":
            continue
        if task_has_resume_marker(task):
            continue
        now = utcnow()
        task.pipeline_status = "backlog"
        task.runtime.current_stage = idle_stage_state(updated_at=now, stage="backlog")
        task.runtime.updated_at = now
        if task.runtime.last_outcome.kind == "interrupted":
            task.runtime.last_outcome.stage = "backlog"
        mutated.append(task)
    return mutated


def set_active_task(root: Path, task_id: str | None) -> WorkspaceState:
    from litehive.state.records import require_task
    from litehive.tasks.archive import get_archived_task
    from litehive.state.locking import workspace_lock, workspace_mutation_guard
    from litehive.state.persist import load_state, save_state
    from litehive.state.persist import persist_task_and_state

    with workspace_mutation_guard(root), workspace_lock(root):
        if task_id is not None and get_archived_task(root, task_id) is not None:
            raise ValueError(
                f"Task {task_id} is archived and cannot be switched active. "
                "Create a new task for follow-up work instead."
            )
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
    from litehive.state.locking import workspace_lock, workspace_mutation_guard
    from litehive.state.persist import load_state, persist_tasks_and_state, save_state
    from litehive.recovery.execution_recovery import recover_stale_runner_state

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
    from litehive.state.records import list_tasks
    from litehive.state.locking import workspace_lock, workspace_mutation_guard
    from litehive.state.persist import load_state
    from litehive.recovery.execution_recovery import recover_stale_runner_state

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
            simulated_task.status = "done"
            simulated_task.pipeline_status = "done"


def dequeue_next_task(root: Path) -> TaskRecord | None:
    return dequeue_next_task_selection(root).task


def dequeue_next_task_selection(root: Path) -> TaskSelection:
    from litehive.state.locking import workspace_mutation_guard
    from litehive.state.persist import persist_tasks_and_state, save_state
    from litehive.recovery.execution_recovery import recover_stale_runner_state
    from .reports import record_recovery_report
    from litehive.state.persist import persist_task_and_state

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
            if next_task.status == "flagged":
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
                # Drop the v2 pipeline_task_state row so the runner starts
                # from `ready` instead of re-emitting the sticky `failed`
                # terminal and looping forever.
                from litehive.lifecycle.persistence import SqlitePersistence

                SqlitePersistence(root).reset(next_task.id)
            if next_task.status in {"queued", "interrupted"}:
                next_task.status = "in_progress"
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
    return task.status == "parked"


def is_task_eligible_for_execution(task: TaskRecord) -> bool:
    if has_blocking_failed_run_history(task):
        return False
    if _has_terminal_execution_status(task):
        return False
    if _has_terminal_outcome_kind(task):
        return False
    if task.pipeline_status == "done":
        return False
    if _needs_manual_intervention(task):
        return False
    if _is_recovery_budget_exhausted(task):
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
    from litehive.state.records import list_tasks

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
    return is_task_eligible_for_execution(task) and (task.status == "in_progress" or task.pipeline_status != "backlog")


def _task_selection_key(
    task: TaskRecord,
    *,
    queue_index: int,
    queue: list[str],
    tasks_by_id: dict[str, TaskRecord],
) -> tuple[int | str, ...]:
    interrupted_rank = 0 if _is_interrupted_task(task) else 1
    return (
        queue_index,
        -_dependent_task_count(task.id, queue, tasks_by_id),
        interrupted_rank,
        task.id,
    )


def _resolve_next_task_from_state(
    root: Path, state: WorkspaceState
) -> tuple[TaskRecord | None, list[BlockedTask], bool, list[TaskRecord]]:
    from litehive.state.records import list_tasks
    from litehive.state.store import runtime_store
    from litehive.recovery.detection import TaskLaunchFailure

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
        if task.status == "in_progress" or resumable_queue_stage(task) is not None:
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
    return set_active_task(root, None)


def restore_untouched_active_task(root: Path) -> WorkspaceState:
    from litehive.state.records import get_task
    from litehive.state.locking import workspace_mutation_guard
    from litehive.state.persist import save_state
    from litehive.recovery.execution_recovery import (
        prepare_interrupted_task,
        stale_interruption_reason,
        interruption_journal_message,
    )
    from litehive.state.persist import persist_task_and_state

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

        if task is not None and is_task_eligible_for_execution(task) and task.runtime.execution_status != "running":
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
    from litehive.state.records import list_tasks
    from litehive.state.persist import load_state

    markers: dict[str, list[str]] = {}
    current_state = state or load_state(root)
    tasks = list_tasks(root, strict=False)
    tasks_by_id = {task.id: task for task in tasks}
    active_task = None if current_state.active_task_id is None else tasks_by_id.get(current_state.active_task_id)
    if active_task is not None and (
        is_task_eligible_for_execution(active_task) or active_task.runtime.execution_status == "running"
    ):
        markers.setdefault(current_state.active_task_id, []).append("workspace.active_task_id")
    for task in tasks:
        if task.status == "in_progress" and task.pipeline_status != "done" and is_task_eligible_for_execution(task):
            markers.setdefault(task.id, []).append("task.status=in_progress")
        if task.runtime.execution_status == "running":
            markers.setdefault(task.id, []).append("runtime.execution_status=running")
    return markers


def validate_single_active_task(root: Path, state: WorkspaceState | None = None) -> None:
    markers = active_task_markers(root, state)
    if len(markers) <= 1:
        return
    details = "; ".join(f"{task_id} ({', '.join(task_markers)})" for task_id, task_markers in sorted(markers.items()))
    raise WorkspaceConflictError(
        f"workspace has multiple active tasks: {details}. Clear the stale active task state before running again."
    )
