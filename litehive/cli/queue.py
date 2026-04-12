from litehive.config import ensure_workspace
from litehive.git_ops import GitError, checkpoint_message
from litehive.recovery.execution_recovery import recover_completed_task
from litehive.tasks.archive import archive_done_tasks, archive_task, cleanup_archived_tasks
from litehive.tasks.crud import require_task
from litehive.tasks.models import WorkspaceConflictError
from litehive.tasks.normalization import missing_acceptance_criteria_reason
from litehive.tasks.persistence import load_state
from litehive.tasks.queue_management import move_queued_task, prioritize_queued_tasks
from litehive.workspace.task_status import (
    abandon_task,
    close_task,
    requeue_task,
    resume_task,
    stop_current_task,
    switch_task_engine,
)


def cmd_recover(task_id, workspace):
    ensure_workspace(workspace)
    try:
        task = recover_completed_task(workspace, task_id)
    except (GitError, WorkspaceConflictError) as exc:
        print(f"recover failed: {exc}")
        return 1

    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_status: {task.pipeline_status}")
    print("recovery_policy: recover requeued the task without reverting workspace code")
    print(f"next_commit_message: {checkpoint_message(task)}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


def cmd_move(task_id, position, workspace):
    ensure_workspace(workspace)
    try:
        state = move_queued_task(workspace, task_id, position)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"move failed: {exc}")
        return 1
    print(f"task_id: {task_id}")
    print(f"position: {state.queue.index(task_id) + 1}")
    return 0


def cmd_promote(task_id, workspace):
    ensure_workspace(workspace)
    try:
        task = require_task(workspace, task_id)
        if task.status in {
            "interrupted",
            "parked",
            "flagged",
            "cancelled",
            "wont_do",
            "deferred",
            "duplicate",
        }:
            task = resume_task(workspace, task_id, front=True)
            print(f"task: {task.id} {task.title}")
            print("status: queued")
            print(f"pipeline_status: {task.pipeline_status}")
            missing_criteria_reason = missing_acceptance_criteria_reason(task)
            if missing_criteria_reason is not None:
                print(f"warning: {missing_criteria_reason}")
            print("position: 1")
            return 0
        move_queued_task(workspace, task_id, 1)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"promote failed: {exc}")
        return 1
    print(f"task_id: {task_id}")
    print("position: 1")
    return 0


def cmd_prioritize(task_ids, workspace):
    ensure_workspace(workspace)
    try:
        state = prioritize_queued_tasks(workspace, task_ids)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"prioritize failed: {exc}")
        return 1
    print(f"moved_tasks: {' '.join(task_ids)}")
    print(f"moved_count: {len(task_ids)}")
    print(f"front_of_queue: {' '.join(state.queue[: len(task_ids)])}")
    print(f"queue_length: {len(state.queue)}")
    return 0


def cmd_requeue_task(task_id, workspace, front: bool = False, force: bool = False):
    ensure_workspace(workspace)
    try:
        task = requeue_task(workspace, task_id, front=front, force=force)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"requeue failed: {exc}")
        return 1
    state = load_state(workspace)
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_status: {task.pipeline_status}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    print(f"position: {state.queue.index(task.id) + 1}")
    return 0


def cmd_queue_requeue(task_id, workspace, front: bool = False, force: bool = False):
    ensure_workspace(workspace)
    task = require_task(workspace, task_id)
    if task.pipeline_status == "done" or task.status == "done":
        return cmd_recover(task_id, workspace)
    return cmd_requeue_task(task_id, workspace, front=front, force=force)


def cmd_resume_task(task_id, workspace, front: bool = False):
    ensure_workspace(workspace)
    try:
        task = resume_task(workspace, task_id, front=front)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"resume failed: {exc}")
        return 1
    state = load_state(workspace)
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_status: {task.pipeline_status}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    print(f"position: {state.queue.index(task.id) + 1}")
    return 0


def cmd_abandon_task(task_id, workspace):
    ensure_workspace(workspace)
    try:
        task = abandon_task(workspace, task_id)
    except ValueError as exc:
        print(f"abandon failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print("status: cancelled")
    print(f"pipeline_status: {task.pipeline_status}")
    return 0


def cmd_stop_task(workspace):
    ensure_workspace(workspace)
    try:
        summary = stop_current_task(workspace)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"stop failed: {exc}")
        return 1
    print(f"task: {summary.task.id} {summary.task.title}")
    print(f"status: {summary.task.status}")
    print(f"pipeline_status: {summary.task.pipeline_status}")
    print(f"runner_pid: {summary.runner_pid if summary.runner_pid is not None else '-'}")
    print(f"signal_sent: {'yes' if summary.signal_sent else 'no'}")
    return 0


def cmd_switch_task(task_id, engine, workspace, reason):
    ensure_workspace(workspace)
    try:
        summary = switch_task_engine(
            workspace,
            task_id,
            engine=engine,
            reason=reason,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"switch failed: {exc}")
        return 1
    state = load_state(workspace)
    print(f"task: {summary.task.id} {summary.task.title}")
    print("status: queued")
    print(f"pipeline_status: {summary.task.pipeline_status}")
    print(f"engine: {summary.previous_engine} -> {summary.new_engine}")
    print(f"was_active: {'yes' if summary.was_active else 'no'}")
    print(f"runner_pid: {summary.runner_pid if summary.runner_pid is not None else '-'}")
    print(f"signal_sent: {'yes' if summary.signal_sent else 'no'}")
    print(f"position: {state.queue.index(summary.task.id) + 1}")
    return 0


def cmd_close_task(task_id, workspace, outcome, reason=None, follow_up_task=None):
    from litehive.cli.agent_cli import block_if_agent

    block_if_agent()
    ensure_workspace(workspace)
    try:
        task = close_task(
            workspace,
            task_id,
            outcome=outcome,
            reason=reason,
            follow_up_task_id=follow_up_task,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"close failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print(f"status: {task.status}")
    print(f"outcome: {task.runtime.last_outcome.reason_code}")
    print(f"reason: {task.runtime.last_outcome.reason}")
    print(f"follow_up_task: {task.runtime.last_outcome.follow_up_task_id or '-'}")
    print(f"pipeline_status: {task.pipeline_status}")
    return 0


def cmd_archive(workspace, task_id=None, all_done: bool = False, command_parser=None):
    ensure_workspace(workspace)
    if task_id is None and not all_done:
        parser = command_parser
        if parser is not None:
            parser.print_help()
        return 2
    try:
        if task_id is not None:
            task = archive_task(workspace, task_id)
            print(f"archived: {task.id} {task.title}")
            print("archived_count: 1")
        else:
            tasks = archive_done_tasks(
                workspace,
                on_skip=lambda task_id, exc: print(
                    f"archive skipped: {task_id} ({exc})"
                ),
            )
            for task in tasks:
                print(f"archived: {task.id} {task.title}")
            print(f"archived_count: {len(tasks)}")
    except ValueError as exc:
        print(f"archive failed: {exc}")
        return 1
    return 0


def cmd_cleanup(workspace, older_than):
    ensure_workspace(workspace)
    try:
        deleted = cleanup_archived_tasks(workspace, older_than)
    except ValueError as exc:
        print(f"cleanup failed: {exc}")
        return 1
    for task in deleted:
        print(f"deleted: {task.id} {task.title}")
    print(f"deleted_count: {len(deleted)}")
    return 0
