from pathlib import Path
from typing import Annotated, cast
import os
import sys
import json
from dataclasses import dataclass

import click
import typer

from litehive.cli.agent_cli import block_if_agent
from litehive.cli.common import WorkspaceOption, choice, require_subcommand
from litehive.config.engine_models import select_engine_for_workspace
from litehive.config.paths import workspace_path
from litehive.config.runtime_settings import load_runtime_setting_audit_entries, load_runtime_settings
from litehive.config.workspace import ensure_workspace, normalize_workspace_root, resolve_workspace
from litehive.container import LitehiveContainer, build_container, build_workspace
from heru import ENGINE_CHOICES
from litehive.daemon.execution import (
    daemon_status_lines,
    run_daemon_loop,
    start_background_daemon,
    stop_workspace_daemon,
)
from litehive.daemon.registry import get_workspace_daemon
from litehive.db.schema import MigrationApplyError, apply_pending_migrations, migration_status
from litehive.git.ops import has_non_litehive_changes, is_git_repo
from litehive.domain.common import PipelineState
from litehive.domain.reports import TaskActivityEntry, TaskActivityVerdict
from litehive.lifecycle.orchestration import ExecutionResult, run_task_for_workspace
from litehive.state.backup import create_workspace_backup, list_workspace_backups, restore_workspace_backup
from litehive.state.records import get_task
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.state.persist import (
    CONSECUTIVE_TASK_FAILURE_STOP_REASON,
    load_state,
    record_task_completion,
    set_pool_stop_reason,
)
from litehive.tasks.event_log import rebuild_sqlite_from_task_event_log
from litehive.tasks.queue import dequeue_next_task, peek_next_task_selection
from litehive.tasks.activity import append_task_activity
from litehive.tasks.audit import load_task_audit_entries
from litehive.state.locking import runner_status


def register_root_commands(app: typer.Typer, backup_app: typer.Typer, db_app: typer.Typer) -> None:
    """
    Wire runner, backup, and db commands onto the top-level Typer app.

    Called once during CLI bootstrap from ``litehive.cli.app``. The
    explicit registration call keeps command wiring out of the
    module top-level so importing this module has no side effects
    on the Typer registry — important for tests and for any tool
    that imports ``runner`` purely for its helpers.
    """
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
    db_app.command("rebuild-from-events", help="Rebuild task tables from the append-only task event log")(
        db_rebuild_from_events
    )
    db_app.command("audit", help="Show durable task audit log entries from the workspace database")(db_audit)
    db_app.command("settings", help="Show audited runtime settings from the workspace database")(db_settings)
    db_app.command("settings-audit", help="Show runtime settings audit log entries")(db_settings_audit)


def start(workspace: WorkspaceOption = Path.cwd()) -> int:
    """
    Boot the background daemon for a workspace.

    Operator entry point for ``litehive start``. Applies any pending
    DB migrations before forking so the daemon never inherits an
    out-of-date schema, then double-forks via
    :func:`start_background_daemon` so the operator's shell returns
    immediately with the new daemon's PID.
    """
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
    """
    Print daemon liveness lines for a workspace.

    Used by ``litehive daemon status`` and exists as a separate
    function (rather than inlined in the daemon CLI module) so
    tests can drive it directly without going through the Typer
    command machinery.
    """
    ensure_workspace(workspace)
    for line in daemon_status_lines(workspace):
        print(line)
    return 0


def stop(workspace: WorkspaceOption = Path.cwd()) -> int:
    """
    Halt the workspace daemon and report the previous PID.

    Operator entry point for ``litehive stop``. Returns exit code
    1 when no daemon was running so a script chaining ``stop`` and
    ``start`` can detect the "nothing to stop" case rather than
    silently treating it as success.
    """
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
    """
    Stop and re-launch the workspace daemon.

    Applies any pending DB migrations between the stop and start
    so the new daemon never inherits a schema older than what its
    code expects. Reports both the previous and new PID so the
    operator can confirm the restart actually replaced the
    process.
    """
    previous = stop_workspace_daemon(workspace)
    apply_pending_migrations(workspace)
    try:
        pid = start_background_daemon(workspace)
    except RuntimeError as exc:
        print(f"daemon restart failed: {exc}")
        return 1
    if previous is not None:
        previous_pid_label = previous.get("pid")
    else:
        previous_pid_label = "-"
    print(f"workspace: {workspace.resolve()}")
    print(f"previous_pid: {previous_pid_label}")
    print(f"pid: {pid}")
    print("daemon_status: running")
    return 0


def daemon_worker(workspace):
    """
    Run the daemon loop in-process for the foreground/worker entry.

    Used by ``litehive daemon worker`` after the supervisor has
    already detached. ``output_stream=None`` suppresses the
    stdout banner the foreground ``start`` path prints because
    the worker process has no controlling terminal.
    """
    return run_daemon_loop(workspace, output_stream=None)


@dataclass(slots=True)
class _RunCommandIteration:
    """
    One pass of :func:`run_once`.

    Carries the exit code plus pool-state signals
    (``consecutive_task_failures``, ``pool_stop_reason``) so the
    drain loop and the single-shot path can decide whether to
    continue or stop without re-reading state from the database.
    """

    exit_code: int
    ran_task: bool
    final_stage: PipelineState | None = None
    consecutive_task_failures: int = 0
    pool_stop_reason: str | None = None


def run_task(
    container: LitehiveContainer,
    task,
    engine_override: str | None = None,
    model_override: str | None = None,
) -> ExecutionResult:
    """
    CLI runner seam around injected task orchestration.

    Tests monkey-patch this module-level name to avoid spinning up the
    pipeline; production passes the already-built container through so the
    orchestration layer does not rebuild workspace dependencies.
    """
    return run_task_for_workspace(
        container.workspace,
        container.config,
        task,
        engine_override=engine_override,
        model_override=model_override,
    )


def run_once(
    container: LitehiveContainer,
    engine: str | None = None,
    model: str | None = None,
) -> _RunCommandIteration:
    """
    Dequeue and execute the next task once.

    Surfaces pool-stop signals to the caller so the drain loop and
    single-shot path can share this code without each repeating the
    "should we keep going?" logic. Also short-circuits when the
    pool is already in the consecutive-failure stop state so a
    known-bad pool does not eat another task.
    Used by ``litehive run``, the drain loop, and exercised
    directly by the hook-reject circuit-breaker tests.
    """
    workspace = container.workspace
    root = workspace.root
    stopped_iteration = _existing_consecutive_task_failure_stop(root)
    if stopped_iteration is not None:
        return stopped_iteration

    while True:
        try:
            task = dequeue_next_task(workspace)
        except WorkspaceConflictError as exc:
            print(f"run failed: {exc}")
            return _RunCommandIteration(exit_code=1, ran_task=False)
        except Exception as exc:
            print(f"run failed: {exc}")
            return _RunCommandIteration(exit_code=1, ran_task=False)

        if task is None:
            state = load_state(root)
            return _RunCommandIteration(
                exit_code=0,
                ran_task=False,
                consecutive_task_failures=state.consecutive_task_failures,
                pool_stop_reason=state.pool_stop_reason,
            )

        try:
            result = run_task(
                container,
                task,
                engine_override=engine,
                model_override=model,
            )
        except Exception as exc:
            print(f"run failed: {exc}")
            return _RunCommandIteration(exit_code=1, ran_task=False)

        if result.task is not None:
            print(f"task: {result.task.id} {result.task.title}")
        print(f"final_stage: {result.final_stage}")
        if result.failed_reason:
            print(f"failed_reason: {result.failed_reason}")
        if result.failed_message:
            print(f"failed_message: {result.failed_message}")
        consecutive_task_failures, pool_stop_reason = record_task_completion(
            root,
            final_stage=result.final_stage,
        )
        if pool_stop_reason == CONSECUTIVE_TASK_FAILURE_STOP_REASON:
            _emit_consecutive_task_failure_stop(consecutive_task_failures)
        return _RunCommandIteration(
            exit_code=0,
            ran_task=True,
            final_stage=result.final_stage,
            consecutive_task_failures=consecutive_task_failures,
            pool_stop_reason=pool_stop_reason,
        )


def _existing_consecutive_task_failure_stop(workspace: Path) -> _RunCommandIteration | None:
    """
    Short-circuit ``run_once`` when the pool is already in the failure-stop state.

    Without this guard the runner would dequeue another task into
    a known-bad pool and either fail it again or silently skip it.
    Returns the synthetic iteration result that ``run_once`` would
    otherwise produce after the next failure, so the caller's
    handling of the stopped state stays uniform.
    """
    state = load_state(workspace)
    if state.pool_stop_reason != CONSECUTIVE_TASK_FAILURE_STOP_REASON:
        return None
    _emit_consecutive_task_failure_stop(state.consecutive_task_failures)
    return _RunCommandIteration(
        exit_code=0,
        ran_task=False,
        consecutive_task_failures=state.consecutive_task_failures,
        pool_stop_reason=state.pool_stop_reason,
    )


def _emit_consecutive_task_failure_stop(consecutive_task_failures: int) -> None:
    """
    Print the operator-visible banner when the pool stops after repeated failures.

    Floors the failure count at 3 so a stale counter cannot show
    ``stopped after 0`` (or other below-threshold numbers) which
    would look like a glitch rather than the actual circuit-breaker
    behavior. Three is the threshold the pool stop reason itself
    represents.
    """
    failure_count = max(3, int(consecutive_task_failures))
    print(f"critical_status: stopped after {failure_count} consecutive task failures")


def _run_single(
    container: LitehiveContainer,
    engine: str | None = None,
    model: str | None = None,
) -> int:
    """
    Single-shot ``litehive run`` path.

    Dispatches one :func:`run_once` and surfaces either the
    pool-stopped banner or a "no queued task" message — without
    that explicit message the operator would otherwise see a
    silent zero exit code on an empty queue. The operator default
    when neither ``--drain`` nor ``--dry-run`` is given.
    """
    iteration = run_once(container, engine=engine, model=model)
    if iteration.pool_stop_reason == CONSECUTIVE_TASK_FAILURE_STOP_REASON:
        print(f"Pool stopped: {CONSECUTIVE_TASK_FAILURE_STOP_REASON}")
        return iteration.exit_code
    if not iteration.ran_task:
        print("No queued task.")
    return iteration.exit_code


def _preview_single(
    container: LitehiveContainer,
    engine: str | None = None,
    model: str | None = None,
) -> int:
    """
    Implement ``litehive run --dry-run``.

    Peeks the queue selector and the engine selector without
    actually launching the task. Lets an operator confirm which
    task and engine the next run would pick before committing —
    valuable when triaging engine quotas or freezes that may have
    pushed the choice somewhere unexpected.
    """
    workspace = container.workspace
    selection = peek_next_task_selection(workspace)
    if selection.task is None:
        state = load_state(workspace.root)
        if state.queue:
            print("No runnable task.")
        else:
            print("No queued task.")
        return 0

    engine_selection = select_engine_for_workspace(
        workspace,
        selection.task,
        container.config,
        engine_override=engine,
        model_override=model,
    )
    print(f"task: {selection.task.id} {selection.task.title}")
    if engine_selection.engine_name is None:
        print("engine: unavailable")
        if engine_selection.blocked_reason:
            print(f"reason: {engine_selection.blocked_reason}")
        return 0
    print(f"engine: {engine_selection.engine_name}")
    if engine_selection.model_name is not None:
        print(f"model: {engine_selection.model_name}")
    return 0


def _workspace_has_dirty_non_litehive_changes(workspace: Path) -> bool:
    """
    Detect uncommitted user-owned changes in the workspace tree.

    Ignores litehive's own runtime files (``.litehive/`` and
    workspace state) because those are expected to churn during
    normal operation. Used by the drain loop's
    ``--stop-on-dirty-git`` policy so the pool refuses to run on
    top of in-flight operator edits, which would otherwise be
    swept into a checkpoint commit.
    """
    if not is_git_repo(workspace):
        return False
    return has_non_litehive_changes(workspace)


def _run_drain(
    container: LitehiveContainer,
    engine: str | None,
    model: str | None,
    stop_on_failure: bool,
    max_tasks: int | None,
    stop_on_dirty_git: bool,
) -> int:
    """
    Drive ``litehive run --drain``.

    Loops :func:`run_once` until the queue empties, the failure
    cap is hit, ``max_tasks`` is reached, or the workspace turns
    dirty. Records the chosen stop reason on the pool before
    returning so subsequent invocations and status displays can
    explain why the pool quit; without that record the operator
    would have to guess from the printed banner alone.
    """
    root = container.workspace.root
    tasks_run = 0
    while True:
        if stop_on_dirty_git and _workspace_has_dirty_non_litehive_changes(root):
            set_pool_stop_reason(root, "dirty_git_state")
            print("Pool stopped: dirty_git_state")
            return 0

        iteration = run_once(container, engine=engine, model=model)
        if iteration.exit_code != 0:
            return iteration.exit_code
        if iteration.pool_stop_reason == CONSECUTIVE_TASK_FAILURE_STOP_REASON:
            print(f"Pool stopped: {CONSECUTIVE_TASK_FAILURE_STOP_REASON}")
            return 0
        if not iteration.ran_task:
            if tasks_run == 0:
                state = load_state(root)
                if state.queue:
                    print("No runnable task.")
                else:
                    print("No queued task.")
            return 0

        tasks_run += 1
        if stop_on_failure and iteration.final_stage != PipelineState.DONE:
            set_pool_stop_reason(root, "failure_detected")
            print("Pool stopped: failure_detected")
            return 0
        if max_tasks is not None and tasks_run >= max_tasks:
            set_pool_stop_reason(root, "max_tasks_reached")
            print("Pool stopped: max_tasks_reached")
            return 0


def run_command(
    workspace: WorkspaceOption = Path.cwd(),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview the next task without running it")] = False,
    drain: Annotated[bool, typer.Option("--drain", help="Drain the task pool")] = False,
    engine: Annotated[str | None, typer.Option(click_type=choice(ENGINE_CHOICES), help="Override the engine")] = None,
    model: Annotated[str | None, typer.Option(help="Override the model")] = None,
    stop_on_failure: Annotated[bool | None, typer.Option("--stop-on-failure", flag_value=True)] = None,
    max_tasks: Annotated[int | None, typer.Option(help="Stop after this many tasks")] = None,
    stop_on_dirty_git: Annotated[bool | None, typer.Option("--stop-on-dirty-git", flag_value=True)] = None,
) -> int:
    """
    Run, preview, or drain queued tasks.

    Operator entry point for ``litehive run``. Resolves the
    pool-stop policy from CLI flags falling back to workspace
    config (so the operator can override config without editing
    files), then dispatches to single-shot, dry-run preview, or
    drain-loop execution. ``--dry-run`` and ``--drain`` are
    mutually exclusive because previewing a multi-task drain has
    no defined meaning.
    """
    ensure_workspace(workspace)
    if dry_run and drain:
        raise click.UsageError("--dry-run cannot be combined with --drain")
    container = build_container(workspace)
    if dry_run:
        return _preview_single(container, engine=engine, model=model)
    config = container.config
    if stop_on_failure is None:
        effective_stop_on_failure = config.pool_stop_on_failure
    else:
        effective_stop_on_failure = stop_on_failure
    if max_tasks is None:
        effective_max_tasks = config.pool_max_tasks
    else:
        effective_max_tasks = max_tasks
    if stop_on_dirty_git is None:
        effective_stop_on_dirty_git = config.pool_stop_on_dirty_git
    else:
        effective_stop_on_dirty_git = stop_on_dirty_git
    if drain:
        return _run_drain(
            container,
            engine=engine,
            model=model,
            stop_on_failure=effective_stop_on_failure,
            max_tasks=effective_max_tasks,
            stop_on_dirty_git=effective_stop_on_dirty_git,
        )
    return _run_single(container, engine=engine, model=model)


def report_command(
    verdict: Annotated[
        str,
        typer.Option(
            click_type=choice(["advance", "blocked", "budget_hit", "comment", "done", "pass", "reject", "resume"])
        ),
    ],
    message: Annotated[str, typer.Option(help="Detailed explanation (use - for stdin)")] = "",
    message_file: Annotated[Path | None, typer.Option("--message-file", help="Read message from file")] = None,
    role: Annotated[str, typer.Option(help="Role submitting the report")] = "swe",
    stage: Annotated[str | None, typer.Option(help="Stage name")] = None,
    task_id: Annotated[str | None, typer.Option(help="Task ID")] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Repository root containing .litehive/"),
    ] = None,
    files_changed: Annotated[list[str] | None, typer.Option(help="Changed paths; repeat for multiple")] = None,
) -> int:
    """
    Append a stage verdict (advance/reject/done/...) to a task's activity log.

    Operator entry point for ``litehive report``; agents are
    blocked at the top of the function so the broader verdict
    surface here cannot be used to bypass the role-gated
    ``agent report``. Resolves the target task from ``--task-id``,
    ``LITEHIVE_TASK_ID``, or the workspace's active task — in that
    order — so the same command works inside a hook script and
    from a plain operator shell.
    """
    block_if_agent()
    if message == "-":
        message = sys.stdin.read()
    elif message_file is not None:
        message = message_file.read_text(encoding="utf-8")
    if not task_id:
        task_id = os.environ.get("LITEHIVE_TASK_ID")
    try:
        if workspace is None:
            root = resolve_workspace(task_id)
        else:
            root = normalize_workspace_root(workspace, source="--workspace")
    except ValueError as exc:
        print(f"report failed: {exc}")
        return 1
    workspace_obj = build_workspace(root)
    root = workspace_obj.root
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
    stage = stage or task.pipeline_status
    # ``verdict`` is constrained to TaskActivityVerdict members by the
    # Click ``choice(...)`` declaration above; ``cast`` lets the static
    # type tell the same story without a ``# type: ignore``.
    entry = TaskActivityEntry(
        role=role,
        stage=stage,
        verdict=cast(TaskActivityVerdict, verdict),
        message=message,
        files_changed=list(files_changed or []),
    )
    append_task_activity(workspace_obj, task, entry)
    print(f"task: {task.id}")
    print(f"stage: {stage}")
    print(f"verdict: {verdict}")
    print(f"role: {role}")
    return 0


def backup_group(ctx: typer.Context) -> None:
    """Typer group callback for ``litehive backup``; rejects bare invocation."""
    require_subcommand(ctx)


def backup_create(workspace: WorkspaceOption = Path.cwd()) -> int:
    """
    Snapshot the workspace runtime database.

    Operator entry point for ``litehive backup create``. Produces a
    timestamped, compressed copy of the SQLite store so an operator
    can roll back after a botched migration or accidental
    destructive command — the snapshot is the only path back if
    the live database is corrupted.
    """
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
    """
    List database backup snapshots in a workspace.

    Renders timestamp/size/path for each known backup so an
    operator can pick a timestamp for ``backup restore``. The
    output is line-oriented so it can feed shell pipelines.
    """
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
    """
    Restore a workspace database from a named backup.

    Refuses to run while a daemon or runner is active because
    overwriting the SQLite file under a live writer would corrupt
    in-flight state and mid-stream transactions. Confirms before
    overwriting unless ``--yes`` is set so an interactive operator
    cannot accidentally wipe their workspace.
    """
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


def db_group(ctx: typer.Context) -> None:
    """Typer group callback for ``litehive db``; rejects bare invocation."""
    if ctx.invoked_subcommand is not None:
        return
    require_subcommand(ctx)


def db_status(workspace: WorkspaceOption = Path.cwd()) -> int:
    """
    Print schema version and pending migrations for a workspace.

    Operators read this to know whether ``db migrate`` is needed
    before starting the daemon; running with pending migrations
    leaves the system in an undefined state because newer code can
    rely on schema additions that are not yet in the file.
    """
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
    """
    Apply pending workspace database migrations.

    Operator entry point for ``litehive db migrate``. ``--dry-run``
    prints the migrations that *would* apply without changing the
    file so an operator can rehearse a release before flipping
    schema state on a live workspace.
    """
    workspace = normalize_workspace_root(workspace, source="--workspace")
    try:
        plan = apply_pending_migrations(workspace, dry_run=dry_run)
    except MigrationApplyError as exc:
        print(f"db migrate failed: {exc}")
        return 1
    if plan.dry_run:
        dry_run_label = "yes"
    else:
        dry_run_label = "no"
    print(f"workspace: {workspace}")
    print(f"dry_run: {dry_run_label}")
    if plan.pending_migrations:
        if plan.dry_run:
            label = "would_apply"
        else:
            label = "applied"
        for migration in plan.pending_migrations:
            print(f"{label}: {migration.name}")
    else:
        print("pending_migrations: 0")
    if not plan.dry_run:
        print(f"schema_version: {migration_status(workspace).current_version}")
    return 0


def db_rebuild_from_events(workspace: WorkspaceOption = Path.cwd()) -> int:
    """
    Replay the append-only task event log to rebuild SQLite tables.

    Operator recovery path for when the runtime DB is corrupted or
    lost; the event log is the source of truth for task history,
    so a clean replay reproduces the workspace's task state from
    durable evidence rather than relying on a backup that may be
    older than the latest events.
    """
    workspace = normalize_workspace_root(workspace, source="--workspace")
    container = build_container(workspace)
    try:
        summary = rebuild_sqlite_from_task_event_log(container.workspace)
    except Exception as exc:
        print(f"db rebuild-from-events failed: {exc}")
        return 1
    print(f"workspace: {workspace}")
    print(f"event_log: {summary.event_log_path}")
    print(f"events_seen: {summary.events_seen}")
    print(f"events_replayed: {summary.events_replayed}")
    print(f"invalid_events: {summary.invalid_events}")
    print(f"tasks_rebuilt: {summary.tasks_rebuilt}")
    print(f"queue_length: {summary.queue_length}")
    print(f"audit_entries: {summary.audit_entries}")
    print(f"activity_entries: {summary.activity_entries}")
    print(f"stage_reports: {summary.stage_reports}")
    print(f"recovery_reports: {summary.recovery_reports}")
    return 0


def db_audit(
    task_id: Annotated[str | None, typer.Argument(help="Optional task id to filter")] = None,
    workspace: WorkspaceOption = Path.cwd(),
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum rows to show")] = 20,
    action: Annotated[str | None, typer.Option("--action", help="Optional action filter")] = None,
) -> int:
    """
    Print durable task audit log entries.

    Each entry records a status, queue, or pipeline transition with
    actor and source attribution so operators can trace why a task
    moved between states without correlating multiple log files.
    Both ``task_id`` and ``--action`` filters narrow the output for
    targeted investigations.
    """
    workspace = normalize_workspace_root(workspace, source="--workspace")
    container = build_container(workspace)
    entries = load_task_audit_entries(container.workspace, task_id=task_id, action=action, limit=limit)
    print(f"workspace: {workspace}")
    print(f"audit_entries: {len(entries)}")
    for entry in entries:
        print(f"id: {entry.id}")
        print(f"task_id: {entry.task_id}")
        print(f"created_at: {entry.created_at}")
        print(f"action: {entry.action}")
        print(f"actor: {entry.actor}")
        print(f"source: {entry.source}")
        print(f"task_status: {entry.task_status_before or '-'} -> {entry.task_status_after or '-'}")
        print(f"pipeline_status: {entry.pipeline_status_before or '-'} -> {entry.pipeline_status_after or '-'}")
        print(f"queue_position: {entry.queue_position_before or '-'} -> {entry.queue_position_after or '-'}")
        print(f"context: {json.dumps(entry.context, sort_keys=True)}")
    return 0


def db_settings(workspace: WorkspaceOption = Path.cwd()) -> int:
    """
    Print the audited runtime settings stored in the workspace database.

    Sorted by key so the output is stable across runs, which makes
    diffs between two workspaces (or two snapshots of the same
    workspace) meaningful. Values are JSON-encoded so structured
    settings render unambiguously.
    """
    workspace = normalize_workspace_root(workspace, source="--workspace")
    container = build_container(workspace)
    settings = load_runtime_settings(container.workspace)
    print(f"workspace: {workspace}")
    for key in sorted(settings):
        print(f"{key}: {json.dumps(settings[key], sort_keys=True)}")
    return 0


def db_settings_audit(
    key: Annotated[str | None, typer.Argument(help="Optional runtime setting key to filter")] = None,
    workspace: WorkspaceOption = Path.cwd(),
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum rows to show")] = 20,
) -> int:
    """
    Print the change history of audited runtime settings.

    Each entry shows ``old_value -> new_value`` with actor and
    source attribution so operators can answer "who changed this
    setting and when?" without grepping logs. The optional ``key``
    argument narrows the output to one setting.
    """
    workspace = normalize_workspace_root(workspace, source="--workspace")
    container = build_container(workspace)
    entries = load_runtime_setting_audit_entries(container.workspace, key=key, limit=limit)
    print(f"workspace: {workspace}")
    print(f"setting_audit_entries: {len(entries)}")
    for entry in entries:
        print(f"id: {entry.id}")
        print(f"key: {entry.key}")
        print(f"created_at: {entry.created_at}")
        print(f"actor: {entry.actor}")
        print(f"source: {entry.source}")
        print(f"old_value: {json.dumps(entry.old_value, sort_keys=True)}")
        print(f"new_value: {json.dumps(entry.new_value, sort_keys=True)}")
        print(f"context: {json.dumps(entry.context, sort_keys=True)}")
    return 0
