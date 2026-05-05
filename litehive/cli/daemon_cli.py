from pathlib import Path
from typing import Annotated

import typer

from litehive.cli.common import WorkspaceOption, make_typer, require_subcommand
from litehive.cli.runner import (
    daemon_status as runner_daemon_status,
    daemon_worker as runner_daemon_worker,
    restart as runner_restart,
    start as runner_start,
    stop as runner_stop,
)
from litehive.config.workspace import normalize_workspace_root, resolve_workspace

StatusWorkspaceOption = Annotated[
    Path | None,
    typer.Option("--workspace", help="Repository root containing .litehive/"),
]

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
def daemon_status(workspace: StatusWorkspaceOption = None) -> int:
    try:
        if workspace is None:
            root = resolve_workspace(None, register=False)
        else:
            root = normalize_workspace_root(workspace, source="--workspace")
    except ValueError as exc:
        print(f"daemon status failed: {exc}")
        return 1
    return runner_daemon_status(root)


@app.command("stop", help="Stop the workspace daemon")
def daemon_stop(workspace: WorkspaceOption) -> int:
    return runner_stop(workspace)


@app.command("restart", help="Restart the workspace daemon")
def daemon_restart(workspace: WorkspaceOption) -> int:
    return runner_restart(workspace)


@app.command("worker", hidden=True)
def daemon_worker(workspace: WorkspaceOption) -> int:
    return runner_daemon_worker(workspace)
