from pathlib import Path
from typing import Annotated
import os
import sys
from dataclasses import dataclass

import click
import typer

from litehive.cli.common import WorkspaceOption, choice, require_subcommand
from litehive.config.loading import load_config
from litehive.config.paths import workspace_path
from litehive.config.workspace import ensure_workspace, normalize_workspace_root, resolve_workspace
from heru import ENGINE_CHOICES
from litehive.daemon.execution import (
    daemon_status_lines,
    run_daemon_loop,
    start_background_daemon,
    stop_workspace_daemon,
)
from litehive.daemon.registry import get_workspace_daemon, list_daemon_instances
from litehive.db.schema import MigrationApplyError, apply_pending_migrations, migration_status
from litehive.git.ops import has_non_litehive_changes, is_git_repo
from litehive.domain.reports import TaskActivityEntry
from litehive.domain.task import TaskRecord
from litehive.lifecycle.orchestration import run_task
from litehive.recovery.detection import (
    LaunchFailure,
    TaskLaunchFailure,
    best_effort_recovery_task,
    corrupt_task_launch_diagnostics,
)
from litehive.recovery.execution_recovery import (
    attempt_launch_recovery,
    flag_task_after_failed_launch_recovery,
    prepare_task_launch,
)
from litehive.state.backup import create_workspace_backup, list_workspace_backups, restore_workspace_backup
from litehive.state.records import get_task
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.state.persist import load_state, set_pool_stop_reason
from litehive.tasks.queue import dequeue_next_task, peek_next_task_selection
from litehive.tasks.activity import append_task_activity
from litehive.state.locking import runner_status


def register_root_commands(app: typer.Typer, backup_app: typer.Typer, db_app: typer.Typer) -> None:
    app.command("start", help="Start the background Litehive runner")(start)
    app.command("stop", help="Stop the background Litehive runner")(stop)
    app.command("restart", help="Restart the background Litehive runner")(restart)
    app.command("run", help="Run the next task once")(run_command)
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
    previous = stop_workspace_daemon(workspace)
    apply_pending_migrations(workspace)
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
        print(
            f"{index}. workspace={entry.get('workspace')} pid={entry.get('pid')} started_at={entry.get('started_at')} log_dir={entry.get('log_dir')}"
        )
    return 0


def daemon_worker(workspace):
    return run_daemon_loop(workspace, output_stream=None)


@dataclass(slots=True)
class _RunCommandIteration:
    exit_code: int
    ran_task: bool
    final_stage: str | None = None


def _run_once(
    workspace: Path,
    *,
    engine: str | None = None,
    model: str | None = None,
) -> _RunCommandIteration:
    selection_recovery_attempted = False
    while True:
        try:
            task = dequeue_next_task(workspace)
        except WorkspaceConflictError as exc:
            print(f"run failed: {exc}")
            return _RunCommandIteration(exit_code=1, ran_task=False)
        except Exception as exc:
            recovery_task = best_effort_recovery_task(workspace)
            if recovery_task is None:
                print(f"run failed: {exc}")
                return _RunCommandIteration(exit_code=1, ran_task=False)
            failure = LaunchFailure(
                context="pre_stage_setup_failed",
                summary=f"failed to select the next task: {exc}",
                diagnostics=corrupt_task_launch_diagnostics(workspace, recovery_task.id),
            )
            used_recovery_budget = not selection_recovery_attempted
            if _handle_failed_launch(
                workspace,
                recovery_task,
                failure,
                recovery_attempted=selection_recovery_attempted,
            ):
                selection_recovery_attempted = True
            elif used_recovery_budget:
                selection_recovery_attempted = True
            continue

        if task is None:
            return _RunCommandIteration(exit_code=0, ran_task=False)

        recovery_attempted = False
        launch_recovery_enabled = isinstance(task, TaskRecord)
        while True:
            try:
                if launch_recovery_enabled:
                    prepare_task_launch(workspace, task)
                result = run_task(
                    workspace,
                    task,
                    engine_override=engine,
                    model_override=model,
                )
            except TaskLaunchFailure as exc:
                if not launch_recovery_enabled:
                    raise
                if _handle_failed_launch(
                    workspace,
                    task,
                    exc.as_failure(),
                    recovery_attempted=recovery_attempted,
                ):
                    recovery_attempted = True
                    continue
                break
            except Exception as exc:
                if not launch_recovery_enabled:
                    raise
                if _handle_failed_launch(
                    workspace,
                    task,
                    LaunchFailure(
                        context="pre_stage_setup_failed",
                        summary=f"task launch crashed before stage entry: {exc}",
                    ),
                    recovery_attempted=recovery_attempted,
                ):
                    recovery_attempted = True
                    continue
                break

            if result.task is not None:
                print(f"task: {result.task.id} {result.task.title}")
            print(f"final_stage: {result.final_stage}")
            if result.failed_reason:
                print(f"failed_reason: {result.failed_reason}")
            if result.failed_message:
                print(f"failed_message: {result.failed_message}")
            return _RunCommandIteration(
                exit_code=0,
                ran_task=True,
                final_stage=result.final_stage,
            )


def _handle_failed_launch(
    workspace: Path,
    task,
    failure: LaunchFailure,
    *,
    recovery_attempted: bool,
) -> bool:
    if not recovery_attempted:
        recovery = attempt_launch_recovery(workspace, task, failure)
        if recovery.fixed:
            return True
    flag_task_after_failed_launch_recovery(workspace, task, failure)
    return False


def _run_single(
    workspace: Path,
    *,
    engine: str | None = None,
    model: str | None = None,
) -> int:
    iteration = _run_once(workspace, engine=engine, model=model)
    if not iteration.ran_task:
        print("No queued task.")
    return iteration.exit_code


def _run_dry_run(
    workspace: Path,
    *,
    engine: str | None = None,
    model: str | None = None,
) -> int:
    try:
        selection = peek_next_task_selection(workspace)
    except WorkspaceConflictError as exc:
        print(f"dry-run failed: {exc}")
        return 1
    except Exception as exc:
        print(f"dry-run failed: {exc}")
        return 1

    if selection.task is None:
        print("No queued task.")
        return 0

    task = selection.task
    config = load_config(workspace)

    # Determine effective engine and model
    effective_engine = engine or config.default_engine
    effective_model = model or task.model

    print(f"task: {task.id} {task.title}")
    print(f"stage: {task.pipeline_status}")
    print(f"effective_engine: {effective_engine or '-'}")
    print(f"effective_model: {effective_model or '-'}")

    if selection.blocked:
        print(f"blocked_tasks: {len(selection.blocked)}")
        for blocked in selection.blocked:
            blocked_by_str = ", ".join(blocked.blocked_by)
            print(f"  {blocked.queue_position}. {blocked.task_id} ({blocked.title}) blocked by: {blocked_by_str}")

    return 0


def _workspace_has_dirty_non_litehive_changes(workspace: Path) -> bool:
    if not is_git_repo(workspace):
        return False
    return has_non_litehive_changes(workspace)


def _run_drain(
    workspace: Path,
    *,
    engine: str | None,
    model: str | None,
    stop_on_failure: bool,
    max_tasks: int | None,
    stop_on_dirty_git: bool,
) -> int:
    tasks_run = 0
    while True:
        if stop_on_dirty_git and _workspace_has_dirty_non_litehive_changes(workspace):
            set_pool_stop_reason(workspace, "dirty_git_state")
            print("Pool stopped: dirty_git_state")
            return 0

        iteration = _run_once(workspace, engine=engine, model=model)
        if iteration.exit_code != 0:
            return iteration.exit_code
        if not iteration.ran_task:
            if tasks_run == 0:
                state = load_state(workspace)
                print("No runnable task." if state.queue else "No queued task.")
            return 0

        tasks_run += 1
        if stop_on_failure and iteration.final_stage != "done":
            set_pool_stop_reason(workspace, "failure_detected")
            print("Pool stopped: failure_detected")
            return 0
        if max_tasks is not None and tasks_run >= max_tasks:
            set_pool_stop_reason(workspace, "max_tasks_reached")
            print("Pool stopped: max_tasks_reached")
            return 0


def run_command(
    workspace: WorkspaceOption = Path.cwd(),
    drain: Annotated[bool, typer.Option("--drain", help="Drain the task pool")] = False,
    engine: Annotated[str | None, typer.Option(click_type=choice(ENGINE_CHOICES), help="Override the engine")] = None,
    model: Annotated[str | None, typer.Option(help="Override the model")] = None,
    stop_on_failure: Annotated[bool | None, typer.Option("--stop-on-failure", flag_value=True)] = None,
    max_tasks: Annotated[int | None, typer.Option(help="Stop after this many tasks")] = None,
    stop_on_dirty_git: Annotated[bool | None, typer.Option("--stop-on-dirty-git", flag_value=True)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show which task would be run without executing it")] = False,
) -> int:
    ensure_workspace(workspace)
    config = load_config(workspace)
    if dry_run:
        return _run_dry_run(workspace, engine=engine, model=model)
    effective_stop_on_failure = config.pool_stop_on_failure if stop_on_failure is None else stop_on_failure
    effective_max_tasks = config.pool_max_tasks if max_tasks is None else max_tasks
    effective_stop_on_dirty_git = config.pool_stop_on_dirty_git if stop_on_dirty_git is None else stop_on_dirty_git
    if drain:
        return _run_drain(
            workspace,
            engine=engine,
            model=model,
            stop_on_failure=effective_stop_on_failure,
            max_tasks=effective_max_tasks,
            stop_on_dirty_git=effective_stop_on_dirty_git,
        )
    return _run_single(workspace, engine=engine, model=model)


def report_command(
    verdict: Annotated[str, typer.Option(click_type=choice(["pass", "reject", "comment", "fail"]))] = ...,
    message: Annotated[str, typer.Option(help="Detailed explanation (use - for stdin)")] = "",
    message_file: Annotated[Path | None, typer.Option("--message-file", help="Read message from file")] = None,
    role: Annotated[str, typer.Option(help="Role submitting the report")] = "swe",
    stage: Annotated[str | None, typer.Option(help="Stage name")] = None,
    step: Annotated[str | None, typer.Option("--step", hidden=True)] = None,
    task_id: Annotated[str | None, typer.Option(help="Task ID")] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Repository root containing .litehive/"),
    ] = None,
    files_changed: Annotated[list[str] | None, typer.Option(help="Changed paths; repeat for multiple")] = None,
) -> int:
    from litehive.cli.agent_cli import block_if_agent

    block_if_agent()
    if message == "-":
        message = sys.stdin.read()
    elif message_file is not None:
        message = message_file.read_text(encoding="utf-8")
    if not task_id:
        task_id = os.environ.get("LITEHIVE_TASK_ID")
    try:
        root = (
            resolve_workspace(task_id)
            if workspace is None
            else normalize_workspace_root(workspace, source="--workspace")
        )
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
    stage = stage or step or task.pipeline_status
    normalized_verdict = "reject" if verdict == "fail" else verdict
    comment = TaskActivityEntry(
        role=role,
        stage=stage,
        verdict=normalized_verdict,
        message=message,
        files_changed=list(files_changed or []),
    )
    append_task_activity(root, task, comment)
    print(f"task: {task.id}")
    print(f"stage: {stage}")
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
    database_path = workspace_path(workspace, "data.db")
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
    workspace = normalize_workspace_root(workspace, source="--workspace")
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
    workspace = normalize_workspace_root(workspace, source="--workspace")
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
cmd_report = report_command
cmd_backup_create = backup_create
cmd_backup_list = backup_list
cmd_backup_restore = backup_restore
cmd_db_status = db_status
cmd_db_migrate = db_migrate
