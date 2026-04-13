from pathlib import Path
from typing import Annotated
import os
import sys

import click
import typer

from litehive.cli.display import cli_override_or_default
from litehive.cli.dry_run import plan_pool_dry_run, plan_single_task_dry_run, print_pool_dry_run_plan
from litehive.cli.common import WorkspaceOption, choice, require_subcommand
from litehive.config.loading import load_config
from litehive.config.paths import workspace_database_path
from litehive.config.workspace import ensure_workspace, resolve_workspace
from heru import ENGINE_CHOICES
from litehive.daemon.execution import (
    daemon_status_lines,
    run_daemon_loop,
    start_background_daemon,
    stop_workspace_daemon,
)
from litehive.daemon.registry import get_workspace_daemon, list_daemon_instances
from litehive.db.schema import MigrationApplyError, apply_pending_migrations, migration_status
from litehive.git.ops import GitError, checkpoint_message
from litehive.models.report_models import TaskThreadComment
from litehive.lifecycle.orchestration import run_task
from litehive.recovery.execution_recovery import rollback_completed_task
from litehive.state.backup import create_workspace_backup, list_workspace_backups, restore_workspace_backup
from litehive.state.records import get_task
from litehive.tasks.models import WorkspaceConflictError
from litehive.tasks.normalization import missing_acceptance_criteria_reason
from litehive.tasks.persistence import load_state
from litehive.tasks.queue import dequeue_next_task, peek_next_task_selection, plan_task_selections
from litehive.tasks.reports import append_thread_comment
from litehive.state.locking import runner_status


def register_root_commands(app: typer.Typer, backup_app: typer.Typer, db_app: typer.Typer) -> None:
    app.command("start", help="Start the background Litehive runner")(start)
    app.command("stop", help="Stop the background Litehive runner")(stop)
    app.command("restart", help="Restart the background Litehive runner")(restart)
    app.command("run", help="Run the next task once")(run_command)
    app.command("rollback", help="Revert a task checkpoint commit and requeue the task")(rollback_command)
    app.command("report", help="Submit a stage verdict for the active task")(report_command)
    backup_app.callback()(backup_group)
    backup_app.command("create", help="Create a compressed backup of the workspace runtime database")(backup_create)
    backup_app.command("list", help="List available workspace runtime database backups")(backup_list)
    backup_app.command("restore", help="Restore a workspace runtime database backup")(backup_restore)
    db_app.callback()(db_group)
    db_app.command("status", help="Show workspace database schema version and pending migrations")(db_status)
    db_app.command("migrate", help="Apply pending workspace database migrations")(db_migrate)


def start(workspace: WorkspaceOption = Path.cwd()) -> int:
    foreground = False
    ensure_workspace(workspace)
    apply_pending_migrations(workspace)
    if foreground:
        return run_daemon_loop(workspace, output_stream=sys.stdout)
    try:
        pid = start_background_daemon(workspace)
    except RuntimeError as exc:
        print(f"daemon run failed: {exc}")
        return 1
    print(f"workspace: {workspace.resolve()}")
    print("daemon_status: running")
    print(f"pid: {pid}")
    return 0


def daemon_status(workspace):
    ensure_workspace(workspace)
    for line in daemon_status_lines(workspace):
        print(line)
    return 0


def stop(workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    entry = stop_workspace_daemon(workspace)
    print(f"workspace: {workspace.resolve()}")
    if entry is None:
        print("daemon_status: stopped")
        print("stopped: no")
        return 1
    print(f"pid: {entry.get('pid')}")
    print("daemon_status: stopped")
    print("stopped: yes")
    return 0


def restart(workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    previous = stop_workspace_daemon(workspace)
    try:
        pid = start_background_daemon(workspace)
    except RuntimeError as exc:
        print(f"daemon restart failed: {exc}")
        return 1
    print(f"workspace: {workspace.resolve()}")
    print(f"previous_pid: {previous.get('pid') if previous is not None else '-'}")
    print(f"pid: {pid}")
    print("daemon_status: running")
    return 0


def daemon_instances():
    instances = list_daemon_instances()
    print(f"instances: {len(instances)}")
    for index, entry in enumerate(instances, start=1):
        print(f"{index}. workspace={entry.get('workspace')} pid={entry.get('pid')} started_at={entry.get('started_at')} log_dir={entry.get('log_dir')}")
    return 0


def daemon_worker(workspace):
    ensure_workspace(workspace)
    apply_pending_migrations(workspace)
    return run_daemon_loop(workspace, output_stream=None)


def _run_single_v2(workspace: Path) -> int:
    try:
        task = dequeue_next_task(workspace)
    except WorkspaceConflictError as exc:
        print(f"run failed: {exc}")
        return 1
    if task is None:
        print("No queued task.")
        return 0
    result = run_task(workspace, task)
    if result.task is not None:
        print(f"task: {result.task.id} {result.task.title}")
    print(f"final_stage: {result.final_stage}")
    if result.failed_reason:
        print(f"failed_reason: {result.failed_reason}")
    if result.failed_message:
        print(f"failed_message: {result.failed_message}")
    return 0 if result.final_stage == "done" else 1


def run_command(
    workspace: WorkspaceOption = Path.cwd(),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the planned selection only")] = False,
    drain: Annotated[bool, typer.Option("--drain", help="Drain the task pool")] = False,
    engine: Annotated[str | None, typer.Option(click_type=choice(ENGINE_CHOICES), help="Override the engine")] = None,
    model: Annotated[str | None, typer.Option(help="Override the model")] = None,
    stop_on_failure: Annotated[bool | None, typer.Option("--stop-on-failure", flag_value=True)] = None,
    max_tasks: Annotated[int | None, typer.Option(help="Stop after this many tasks")] = None,
    stop_on_dirty_git: Annotated[bool | None, typer.Option("--stop-on-dirty-git", flag_value=True)] = None,
) -> int:
    ensure_workspace(workspace)
    if dry_run:
        config = load_config(workspace)
        if drain:
            return run_drain_dry_run(workspace, config=config, engine=engine, model=model, stop_on_failure=stop_on_failure, max_tasks=max_tasks, stop_on_dirty_git=stop_on_dirty_git)
        return run_single_dry_run(workspace, config=config, engine=engine, model=model, stop_on_failure=stop_on_failure, max_tasks=max_tasks, stop_on_dirty_git=stop_on_dirty_git)
    return _run_single_v2(workspace)


def run_drain_dry_run(workspace, *, config, engine=None, model=None, stop_on_failure=None, max_tasks=None, stop_on_dirty_git=None):
    try:
        plan = plan_task_selections(workspace)
    except WorkspaceConflictError as exc:
        print(f"run failed: {exc}")
        return 1
    from litehive.config.pool_types import TaskPoolStopConditions

    stop_conditions = TaskPoolStopConditions(
        max_tasks=max_tasks,
        stop_on_failure=cli_override_or_default(stop_on_failure, config.pool_stop_on_failure),
        stop_on_dirty_git=cli_override_or_default(stop_on_dirty_git, config.pool_stop_on_dirty_git),
        stop_on_attention=config.pool_stop_on_attention,
    )
    runnable_tasks, predicted_stop_reason = plan_pool_dry_run(
        workspace,
        planned_tasks=plan.tasks,
        blocked_count=len(plan.blocked),
        config=config,
        stop_conditions=stop_conditions,
        engine_override=engine,
        model_override=model,
    )
    print_pool_dry_run_plan(
        workspace,
        planned_tasks=runnable_tasks,
        blocked=plan.blocked,
        config=config,
        stop_conditions=stop_conditions,
        predicted_stop_reason=predicted_stop_reason,
    )
    return 0


def run_single_dry_run(workspace, *, config, engine=None, model=None, stop_on_failure=None, max_tasks=None, stop_on_dirty_git=None):
    try:
        selection = peek_next_task_selection(workspace)
    except WorkspaceConflictError as exc:
        print(f"run failed: {exc}")
        return 1
    from litehive.config.pool_types import TaskPoolStopConditions

    stop_conditions = TaskPoolStopConditions(
        max_tasks=max_tasks,
        stop_on_failure=cli_override_or_default(stop_on_failure, config.pool_stop_on_failure),
        stop_on_dirty_git=cli_override_or_default(stop_on_dirty_git, config.pool_stop_on_dirty_git),
        stop_on_attention=config.pool_stop_on_attention,
    )
    planned_tasks = [selection.task] if selection.task is not None else []
    runnable_tasks, predicted_stop_reason = plan_single_task_dry_run(
        workspace,
        planned_tasks=planned_tasks,
        blocked_count=len(selection.blocked),
        config=config,
        stop_conditions=stop_conditions,
        engine_override=engine,
        model_override=model,
    )
    print_pool_dry_run_plan(
        workspace,
        planned_tasks=runnable_tasks,
        blocked=selection.blocked,
        config=config,
        stop_conditions=stop_conditions,
        predicted_stop_reason=predicted_stop_reason,
    )
    return 0


def rollback_command(
    task_id: Annotated[str, typer.Argument(help="Task id to roll back")],
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    ensure_workspace(workspace)
    try:
        summary = rollback_completed_task(workspace, task_id)
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


def report_command(
    workspace: Annotated[Path | None, typer.Option("--workspace", help="Repository root containing .litehive/")] = None,
    verdict: Annotated[str, typer.Option(click_type=choice(["pass", "fail", "reject", "comment"]))] = ...,
    message: Annotated[str, typer.Option(help="Detailed explanation")] = ...,
    role: Annotated[str, typer.Option(help="Role submitting the report")] = "swe",
    step: Annotated[str | None, typer.Option(help="Stage name")] = None,
    task_id: Annotated[str | None, typer.Option(help="Task ID")] = None,
    files_changed: Annotated[list[str] | None, typer.Option(help="Changed paths; repeat for multiple")] = None,
) -> int:
    from litehive.cli.agent_cli import block_if_agent

    block_if_agent()
    if not task_id:
        task_id = os.environ.get("LITEHIVE_TASK_ID")
    try:
        root = resolve_workspace(task_id, workspace=workspace)
    except ValueError as exc:
        print(f"report failed: {exc}")
        return 1
    if not task_id:
        state = load_state(root)
        task_id = state.active_task_id
    if not task_id:
        print("report failed: no task id provided, LITEHIVE_TASK_ID is unset, and no active task exists")
        return 1
    task = get_task(root, task_id)
    if task is None:
        print(f"report failed: task {task_id} not found")
        return 1
    step = step or task.pipeline_status
    normalized_verdict = "reject" if verdict == "fail" else verdict
    comment = TaskThreadComment(role=role, step=step, verdict=normalized_verdict, message=message, files_changed=list(files_changed or []))
    append_thread_comment(root, task, comment)
    print(f"task: {task.id}")
    print(f"step: {step}")
    print(f"verdict: {comment.verdict}")
    print(f"role: {role}")
    return 0


def backup_group(ctx: typer.Context) -> None:
    require_subcommand(ctx)


def backup_create(workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    try:
        backup = create_workspace_backup(workspace)
    except Exception as exc:
        print(f"backup create failed: {exc}")
        return 1
    print(f"timestamp: {backup.timestamp}")
    print(f"path: {backup.path}")
    print(f"size_bytes: {backup.size_bytes}")
    return 0


def backup_list(workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    backups = list_workspace_backups(workspace)
    print(f"backups: {len(backups)}")
    for backup in backups:
        print(f"timestamp: {backup.timestamp}")
        print(f"size_bytes: {backup.size_bytes}")
        print(f"path: {backup.path}")
    return 0


def backup_restore(
    timestamp: Annotated[str, typer.Argument(help="Backup timestamp shown by `litehive backup list`")],
    workspace: WorkspaceOption = Path.cwd(),
    yes: Annotated[bool, typer.Option("--yes", help="Skip overwrite confirmation")] = False,
) -> int:
    ensure_workspace(workspace)
    daemon = get_workspace_daemon(workspace)
    if daemon is not None:
        print("backup restore failed: workspace daemon is running")
        return 1
    runner = runner_status(workspace)
    if runner.status in {"running", "late"}:
        print("backup restore failed: workspace runner is active")
        return 1
    database_path = workspace_database_path(workspace)
    if not yes:
        confirmed = click.confirm(f"Restore backup {timestamp} and overwrite {database_path}?", default=False)
        if not confirmed:
            print("restore cancelled")
            return 1
    try:
        backup = restore_workspace_backup(workspace, timestamp)
    except ValueError as exc:
        print(f"backup restore failed: {exc}")
        return 1
    except Exception as exc:
        print(f"backup restore failed: {exc}")
        return 1
    print(f"restored: {backup.timestamp}")
    print(f"path: {backup.path}")
    print(f"database: {database_path}")
    return 0


def db_group(ctx: typer.Context, workspace: WorkspaceOption = Path.cwd()) -> None:
    if ctx.invoked_subcommand is not None:
        return
    require_subcommand(ctx)


def db_status(workspace: WorkspaceOption = Path.cwd()) -> int:
    workspace = resolve_workspace(None, workspace=workspace)
    status = migration_status(workspace)
    print(f"workspace: {workspace}")
    print(f"schema_version: {status.current_version}")
    print(f"applied_migrations: {len(status.applied_migrations)}")
    print(f"pending_migrations: {len(status.pending_migrations)}")
    for migration in status.pending_migrations:
        print(f"pending: {migration.name}")
    return 0


def db_migrate(
    workspace: WorkspaceOption = Path.cwd(),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show pending migrations only")] = False,
) -> int:
    workspace = resolve_workspace(None, workspace=workspace)
    try:
        plan = apply_pending_migrations(workspace, dry_run=dry_run)
    except MigrationApplyError as exc:
        print(f"db migrate failed: {exc}")
        return 1
    print(f"workspace: {workspace}")
    print(f"dry_run: {'yes' if plan.dry_run else 'no'}")
    if plan.pending_migrations:
        label = "would_apply" if plan.dry_run else "applied"
        for migration in plan.pending_migrations:
            print(f"{label}: {migration.name}")
    else:
        print("pending_migrations: 0")
    if not plan.dry_run:
        print(f"schema_version: {migration_status(workspace).current_version}")
    return 0


cmd_run = run_command
cmd_rollback = rollback_command
cmd_report = report_command
cmd_backup_create = backup_create
cmd_backup_list = backup_list
cmd_backup_restore = backup_restore
cmd_db_status = db_status
cmd_db_migrate = db_migrate
