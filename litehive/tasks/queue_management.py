"""Queue management: enqueue, move, prioritize, and recovery preparation."""

from pathlib import Path

from litehive.models import TaskOutcomeState, TaskRecord, WorkspaceState, utcnow

from .crud import require_task, set_task_commit_sha
from .locking import _ensure_future_task_mutation_allowed, _workspace_lock
from .normalization import implementation_entry_stage
from .persistence import _save_state_without_runner_guard, load_state


def enqueue_task(root: Path, task_id: str) -> WorkspaceState:
    return _enqueue_task(root, task_id, front=False)


def enqueue_task_front(root: Path, task_id: str) -> WorkspaceState:
    return _enqueue_task(root, task_id, front=True)


def _enqueue_task(root: Path, task_id: str, *, front: bool) -> WorkspaceState:
    with _workspace_lock(root):
        state = load_state(root)
        _ensure_future_task_mutation_allowed(root, [task_id], state=state)
        state.queue = [item for item in state.queue if item != task_id]
        if front:
            state.queue.insert(0, task_id)
        else:
            state.queue.append(task_id)
        _save_state_without_runner_guard(root, state)
        return state


def move_queued_task(root: Path, task_id: str, position: int) -> WorkspaceState:
    if position < 1:
        raise ValueError("Queue position must be 1 or greater")
    with _workspace_lock(root):
        state = load_state(root)
        _ensure_future_task_mutation_allowed(root, [task_id], state=state)
        if task_id not in state.queue:
            raise ValueError(f"Task {task_id} is not queued")
        queue = [item for item in state.queue if item != task_id]
        target_index = min(position - 1, len(queue))
        queue.insert(target_index, task_id)
        state.queue = queue
        _save_state_without_runner_guard(root, state)
        return state


def prioritize_queued_tasks(root: Path, task_ids: list[str]) -> WorkspaceState:
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
    with _workspace_lock(root):
        state = load_state(root)
        _ensure_future_task_mutation_allowed(root, task_ids, state=state)
        missing = [task_id for task_id in task_ids if task_id not in state.queue]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"Tasks are not queued: {joined}")
        remaining = [queued_id for queued_id in state.queue if queued_id not in task_ids]
        state.queue = [*task_ids, *remaining]
        _save_state_without_runner_guard(root, state)
        return state


def _reset_task_for_recovery(
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
    task.runtime.execution_status = "idle"
    task.runtime.run_started_at = None
    task.runtime.updated_at = now
    task.runtime.active_subagent = None
    task.runtime.interruption = None
    task.runtime.retry_count = 0
    task.runtime.retry_limit = 0
    task.runtime.retry_source = "global"
    if not preserve_continuation_handoff:
        task.runtime.continuation_handoff = None
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": None,
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    if clear_last_outcome:
        task.runtime.last_outcome = TaskOutcomeState()
    elif task.runtime.last_outcome.kind == "interrupted":
        task.runtime.last_outcome.stage = pipeline_status


def _enqueue_recovered_task(state: WorkspaceState, task_id: str) -> None:
    state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
    state.queue.append(task_id)


def prepare_completed_task_for_recovery(task: TaskRecord, *, recovery_stage: str) -> None:
    _reset_task_for_recovery(
        task,
        status="queued",
        pipeline_status=recovery_stage,
    )
    set_task_commit_sha(task, None)
    task.git.rolled_back_checkpoint_attempt = None
