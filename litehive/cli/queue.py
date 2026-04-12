from litehive.config import ensure_workspace
from litehive.git_ops import GitError, checkpoint_message
from litehive.pipeline_old import (
    recover_completed_task,
    inspect_dirty_worktree_gate,
    rollback_completed_task,
)
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
from litehive.tui.app import LitehiveApp


def _cmd_dirty_worktree_gate(args):
    report = inspect_dirty_worktree_gate(args.workspace)
    print(f"workspace: {args.workspace}")
    print(f"dirty_worktree_gate: {'blocked' if report.blocks_pool else 'open'}")
    print(f"clean: {'yes' if report.is_clean else 'no'}")
    if report.is_clean:
        print("details: workspace checkout and recorded task worktrees are clean")
        return 0
    for finding in report.findings:
        print()
        print(f"location_kind: {finding.location_kind}")
        print(f"ownership: {finding.ownership}")
        if finding.task_id is not None:
            print(f"task_id: {finding.task_id}")
        if finding.worktree_path is not None:
            print(f"worktree_path: {finding.worktree_path}")
        print("dirty_paths: " + (", ".join(finding.dirty_paths) if finding.dirty_paths else "-"))
    return 1 if report.blocks_pool else 0


def _cmd_rollback(args):
    ensure_workspace(args.workspace)
    try:
        summary = rollback_completed_task(args.workspace, args.task_id)
    except (GitError, WorkspaceConflictError) as exc:
        print(f"rollback failed: {exc}")
        return 1

    print(f"task: {summary.task.id} {summary.task.title}")
    print(f"rollback_of: {summary.rolled_back_sha}")
    print(f"rollback_commit: {summary.rollback_sha}")
    print("status: queued")
    print(f"pipeline_status: {summary.task.pipeline_status}")
    print("recovery_policy: rollback reverted the checkpoint and requeued the task")
    print(f"next_commit_message: {checkpoint_message(summary.task)}")
    missing_criteria_reason = missing_acceptance_criteria_reason(summary.task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


def _cmd_recover(args):
    ensure_workspace(args.workspace)
    try:
        task = recover_completed_task(args.workspace, args.task_id)
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


def _cmd_move(args):
    ensure_workspace(args.workspace)
    try:
        state = move_queued_task(args.workspace, args.task_id, args.position)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"move failed: {exc}")
        return 1
    print(f"task_id: {args.task_id}")
    print(f"position: {state.queue.index(args.task_id) + 1}")
    return 0


def _cmd_promote(args):
    ensure_workspace(args.workspace)
    try:
        task = require_task(args.workspace, args.task_id)
        if task.status in {
            "interrupted",
            "parked",
            "flagged",
            "cancelled",
            "wont_do",
            "deferred",
            "duplicate",
        }:
            task = resume_task(args.workspace, args.task_id, front=True)
            print(f"task: {task.id} {task.title}")
            print("status: queued")
            print(f"pipeline_status: {task.pipeline_status}")
            missing_criteria_reason = missing_acceptance_criteria_reason(task)
            if missing_criteria_reason is not None:
                print(f"warning: {missing_criteria_reason}")
            print("position: 1")
            return 0
        move_queued_task(args.workspace, args.task_id, 1)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"promote failed: {exc}")
        return 1
    print(f"task_id: {args.task_id}")
    print("position: 1")
    return 0


def _cmd_prioritize(args):
    ensure_workspace(args.workspace)
    try:
        state = prioritize_queued_tasks(args.workspace, args.task_ids)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"prioritize failed: {exc}")
        return 1
    print(f"moved_tasks: {' '.join(args.task_ids)}")
    print(f"moved_count: {len(args.task_ids)}")
    print(f"front_of_queue: {' '.join(state.queue[: len(args.task_ids)])}")
    print(f"queue_length: {len(state.queue)}")
    return 0


def _cmd_requeue_task(args):
    ensure_workspace(args.workspace)
    try:
        task = requeue_task(args.workspace, args.task_id, front=args.front, force=getattr(args, "force", False))
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"requeue failed: {exc}")
        return 1
    state = load_state(args.workspace)
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_status: {task.pipeline_status}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    print(f"position: {state.queue.index(task.id) + 1}")
    return 0


def _cmd_queue_requeue(args):
    ensure_workspace(args.workspace)
    task = require_task(args.workspace, args.task_id)
    if task.pipeline_status == "done" or task.status == "done":
        return _cmd_recover(args)
    return _cmd_requeue_task(args)


def _cmd_resume_task(args):
    ensure_workspace(args.workspace)
    try:
        task = resume_task(args.workspace, args.task_id, front=args.front)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"resume failed: {exc}")
        return 1
    state = load_state(args.workspace)
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_status: {task.pipeline_status}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    print(f"position: {state.queue.index(task.id) + 1}")
    return 0


def _cmd_abandon_task(args):
    ensure_workspace(args.workspace)
    try:
        task = abandon_task(args.workspace, args.task_id)
    except ValueError as exc:
        print(f"abandon failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print("status: cancelled")
    print(f"pipeline_status: {task.pipeline_status}")
    return 0


def _cmd_stop_task(args):
    ensure_workspace(args.workspace)
    try:
        summary = stop_current_task(args.workspace)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"stop failed: {exc}")
        return 1
    print(f"task: {summary.task.id} {summary.task.title}")
    print(f"status: {summary.task.status}")
    print(f"pipeline_status: {summary.task.pipeline_status}")
    print(f"runner_pid: {summary.runner_pid if summary.runner_pid is not None else '-'}")
    print(f"signal_sent: {'yes' if summary.signal_sent else 'no'}")
    return 0


def _cmd_switch_task(args):
    ensure_workspace(args.workspace)
    try:
        summary = switch_task_engine(
            args.workspace,
            args.task_id,
            engine=args.engine,
            reason=args.reason,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"switch failed: {exc}")
        return 1
    state = load_state(args.workspace)
    print(f"task: {summary.task.id} {summary.task.title}")
    print("status: queued")
    print(f"pipeline_status: {summary.task.pipeline_status}")
    print(f"engine: {summary.previous_engine} -> {summary.new_engine}")
    print(f"was_active: {'yes' if summary.was_active else 'no'}")
    print(f"runner_pid: {summary.runner_pid if summary.runner_pid is not None else '-'}")
    print(f"signal_sent: {'yes' if summary.signal_sent else 'no'}")
    print(f"position: {state.queue.index(summary.task.id) + 1}")
    return 0


def _cmd_close_task(args):
    from litehive.cli.agent_cli import block_if_agent

    block_if_agent(allowed_roles={"planner", "reviewer"})
    ensure_workspace(args.workspace)
    try:
        task = close_task(
            args.workspace,
            args.task_id,
            outcome=args.outcome,
            reason=args.reason,
            follow_up_task_id=args.follow_up_task,
        )
    except ValueError as exc:
        print(f"close failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print(f"status: {task.status}")
    print(f"outcome: {task.runtime.last_outcome.reason_code}")
    print(f"reason: {task.runtime.last_outcome.reason}")
    print(f"follow_up_task: {task.runtime.last_outcome.follow_up_task_id or '-'}")
    print(f"pipeline_status: {task.pipeline_status}")
    return 0


def _cmd_archive(args):
    ensure_workspace(args.workspace)
    if args.task_id is None and not getattr(args, "all_done", False):
        parser = getattr(args, "command_parser", None)
        if parser is not None:
            parser.print_help()
        return 2
    try:
        if args.task_id is not None:
            task = archive_task(args.workspace, args.task_id)
            print(f"archived: {task.id} {task.title}")
            print("archived_count: 1")
        else:
            tasks = archive_done_tasks(
                args.workspace,
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


def _cmd_cleanup(args):
    ensure_workspace(args.workspace)
    try:
        deleted = cleanup_archived_tasks(args.workspace, args.older_than)
    except ValueError as exc:
        print(f"cleanup failed: {exc}")
        return 1
    for task in deleted:
        print(f"deleted: {task.id} {task.title}")
    print(f"deleted_count: {len(deleted)}")
    return 0


def _launch_app(workspace, default_mode):
    app = LitehiveApp(workspace=workspace, default_mode=default_mode)
    app.run()
    return 0
