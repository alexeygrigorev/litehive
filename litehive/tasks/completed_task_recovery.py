"""Operator recovery helpers for already-completed tasks."""

from pathlib import Path

from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.task import TaskRecord
from litehive.git.ops import GitError
from litehive.state.persist import load_state
from litehive.tasks.normalization import implementation_entry_stage
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.queue import prepare_completed_task_for_recovery
from litehive.state.locking import workspace_lock, workspace_mutation_guard_for_workspace
from litehive.state.persist import persist_task_and_state
from litehive.workspace import Workspace


def require_completed_task(task: TaskRecord, action: str) -> None:
    """
    Refuse an operator-only ``recover``/``reopen`` action on an unfinished task.

    Surfaces the inconsistency through the CLI rather than corrupting
    in-flight pipeline state: ``recover`` is only meaningful for tasks whose
    lifecycle and pipeline both report ``DONE``.
    """
    if task.status != TaskStatus.DONE or task.pipeline_status != PipelineStatus.DONE:
        raise GitError(f"Task {task.id} is not completed; cannot {action}")


def recover_completed_task(root: Path, task_id: str) -> TaskRecord:
    """
    Path-based compatibility wrapper for completed-task recovery.
    """
    return recover_completed_task_for_workspace(Workspace.from_path(root), task_id)


def recover_completed_task_for_workspace(workspace: Workspace, task_id: str) -> TaskRecord:
    """
    Re-queue a finished task for another implementation pass.

    Resets the pipeline state to the implementation entry stage and records
    the operator-driven transition in the audit journal so a later reader can
    see who reopened the task. Called by ``litehive recover`` when an operator
    decides a closed task needs more work.
    """
    root = workspace.root
    with workspace_mutation_guard_for_workspace(workspace), workspace_lock(root):
        from litehive.state.records import get_task  # noqa: PLC0415
        from litehive.tasks.queue import drop_task_from_workspace_state  # noqa: PLC0415

        task = get_task(root, task_id)
        if task is None:
            raise GitError(f"Task {task_id} not found")
        before_task = snapshot_task_audit_state(task)
        require_completed_task(task, action="recover")
        recovery_stage = implementation_entry_stage(task)
        prepare_completed_task_for_recovery(task, recovery_stage=recovery_stage)
        state = load_state(root)
        queue_before = list(state.queue)
        drop_task_from_workspace_state(state, task.id)
        state.queue.append(task.id)
        persist_task_and_state(
            root,
            task=task,
            state=state,
            journal_message="Task recovered for another implementation pass.",
            audit_entries=[
                build_task_audit_entry(
                    task_id=task.id,
                    action="recovered",
                    actor="operator",
                    source="cli",
                    before_task=before_task,
                    after_task=task,
                    before_queue=queue_before,
                    after_queue=state.queue,
                    context={"recovery_stage": recovery_stage},
                )
            ],
        )
        return task
