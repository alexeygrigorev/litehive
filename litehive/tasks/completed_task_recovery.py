"""Operator recovery helpers for already-completed tasks."""

from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.task import TaskRecord
from litehive.git.ops import GitError
from litehive.state.persist import WorkspaceStateRepository
from litehive.tasks.normalization import implementation_entry_stage
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.queue import TaskQueueService, prepare_completed_task_for_recovery
from litehive.state.locking import WorkspaceMutationGuard, WorkspaceStateLock
from litehive.state.records import WorkspaceTasks
from litehive.workspace import Workspace


def _require_completed_task(task: TaskRecord, action: str) -> None:
    """
    Refuse an operator-only ``recover``/``reopen`` action on an unfinished task.

    Surfaces the inconsistency through the CLI rather than corrupting
    in-flight pipeline state: ``recover`` is only meaningful for tasks whose
    lifecycle and pipeline both report ``DONE``.
    """
    if task.status != TaskStatus.DONE or task.pipeline_status != PipelineStatus.DONE:
        raise GitError(f"Task {task.id} is not completed; cannot {action}")


def _recover_completed_task_transition(workspace: Workspace, task_id: str) -> TaskRecord:
    """
    Re-queue a finished task for another implementation pass.

    Resets the pipeline state to the implementation entry stage and records
    the operator-driven transition in the audit journal so a later reader can
    see who reopened the task. Called by ``litehive recover`` when an operator
    decides a closed task needs more work.
    """
    with WorkspaceMutationGuard(workspace).hold(), WorkspaceStateLock(workspace).hold():
        task = WorkspaceTasks(workspace).get(task_id)
        if task is None:
            raise GitError(f"Task {task_id} not found")
        before_task = snapshot_task_audit_state(task)
        _require_completed_task(task, action="recover")
        recovery_stage = implementation_entry_stage(task)
        prepare_completed_task_for_recovery(task, recovery_stage=recovery_stage)
        state = WorkspaceStateRepository(workspace).load()
        queue_before = list(state.queue)
        TaskQueueService.remove_from_state(state, task.id)
        state.queue.append(task.id)
        WorkspaceStateRepository(workspace).persist_task_and_state(
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
