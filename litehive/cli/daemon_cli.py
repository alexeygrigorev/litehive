import typer

from litehive.cli.common import WorkspaceOption, make_typer, require_subcommand
from litehive.cli.runner import (
    daemon_instances as runner_daemon_instances,
    daemon_status as runner_daemon_status,
    daemon_worker as runner_daemon_worker,
    restart as runner_restart,
    start as runner_start,
    stop as runner_stop,
)

app = make_typer(invoke_without_command=True)


@app.callback()
def daemon_group(ctx: typer.Context) -> None:
    require_subcommand(ctx)


@app.command("run", help="Start the workspace daemon")
def daemon_run(workspace: WorkspaceOption, foreground: bool = False) -> int:
    if foreground:
        return runner_daemon_worker(workspace)
    return runner_start(workspace)


@app.command("status", help="Show daemon state for a workspace")
def daemon_status(workspace: WorkspaceOption) -> int:
    return runner_daemon_status(workspace)


@app.command("stop", help="Stop the workspace daemon")
def daemon_stop(workspace: WorkspaceOption) -> int:
    return runner_stop(workspace)


@app.command("restart", help="Restart the workspace daemon")
def daemon_restart(workspace: WorkspaceOption) -> int:
    return runner_restart(workspace)


@app.command("instances", help="List all live Litehive daemons")
def daemon_instances() -> int:
    return runner_daemon_instances()


@app.command("worker", hidden=True)
def daemon_worker(workspace: WorkspaceOption) -> int:
    return runner_daemon_worker(workspace)
