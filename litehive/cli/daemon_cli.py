import sys

import typer

from litehive.cli.common import WorkspaceOption, make_typer, require_subcommand
from litehive.config.workspace import ensure_workspace
from litehive.daemon.execution import (
    daemon_status_lines,
    run_daemon_loop,
    start_background_daemon,
    stop_workspace_daemon,
)
from litehive.daemon.registry import list_daemon_instances
from litehive.db.schema import apply_pending_migrations

app = make_typer(invoke_without_command=True)


@app.callback()
def daemon_group(ctx: typer.Context) -> None:
    require_subcommand(ctx)


@app.command("run", help="Start the workspace daemon")
def daemon_run(workspace: WorkspaceOption, foreground: bool = False) -> int:
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


@app.command("status", help="Show daemon state for a workspace")
def daemon_status(workspace: WorkspaceOption) -> int:
    ensure_workspace(workspace)
    for line in daemon_status_lines(workspace):
        print(line)
    return 0


@app.command("stop", help="Stop the workspace daemon")
def daemon_stop(workspace: WorkspaceOption) -> int:
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


@app.command("restart", help="Restart the workspace daemon")
def daemon_restart(workspace: WorkspaceOption) -> int:
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


@app.command("instances", help="List all live Litehive daemons")
def daemon_instances() -> int:
    instances = list_daemon_instances()
    print(f"instances: {len(instances)}")
    for index, entry in enumerate(instances, start=1):
        print(
            f"{index}. workspace={entry.get('workspace')} pid={entry.get('pid')} "
            f"started_at={entry.get('started_at')} log_dir={entry.get('log_dir')}"
        )
    return 0


@app.command("worker", hidden=True)
def daemon_worker(workspace: WorkspaceOption) -> int:
    ensure_workspace(workspace)
    apply_pending_migrations(workspace)
    return run_daemon_loop(workspace, output_stream=None)
