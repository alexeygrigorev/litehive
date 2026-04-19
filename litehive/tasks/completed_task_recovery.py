"""Operator recovery helpers for already-completed tasks."""

from pathlib import Path

from litehive.git.ops import GitError
from litehive.domain.task import TaskRecord
from litehive.state.persist import load_state
from litehive.tasks.normalization import implementation_entry_stage
from litehive.tasks.queue import prepare_completed_task_for_recovery
from litehive.state.locking import workspace_lock, workspace_mutation_guard
from litehive.state.persist import persist_task_and_state


def require_completed_task(task: TaskRecord, action: str) -> None:
    if task.status != "done" or task.pipeline_status != "done":
        raise GitError(f"Task {task.id} is not completed; cannot {action}")


def recover_completed_task(root: Path, task_id: str) -> TaskRecord:
    root = root.resolve()
    with workspace_mutation_guard(root), workspace_lock(root):
        from litehive.state.records import get_task

        task = get_task(root, task_id)
        if task is None:
            raise GitError(f"Task {task_id} not found")
        require_completed_task(task, action="recover")
        recovery_stage = implementation_entry_stage(task)
        prepare_completed_task_for_recovery(task, recovery_stage=recovery_stage)
        state = load_state(root)
        state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        state.queue.append(task.id)
        persist_task_and_state(
            root,
            task=task,
            state=state,
            journal_message="Task recovered for another implementation pass.",
        )
        return task
