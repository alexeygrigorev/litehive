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
    """Force the `daemon` group to require a subcommand instead of doing anything when invoked bare."""
    require_subcommand(ctx)


@app.command("run", help="Start the workspace daemon")
def daemon_run(workspace: WorkspaceOption, foreground: bool = False) -> int:
    """Spawn the pool daemon for a workspace; `--foreground` runs the worker loop in this process for development/debugging, otherwise it forks a background daemon."""
    if foreground:
        return runner_daemon_worker(workspace)
    return runner_start(workspace)


@app.command("status", help="Show daemon state for a workspace")
def daemon_status(workspace: StatusWorkspaceOption = None) -> int:
    """Report whether a daemon is running for the given workspace; accepts an optional `--workspace` so it can be invoked from outside a registered repo (e.g. monitoring scripts)."""
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
    """Signal the running daemon to exit cleanly so a stuck or stale pool can be replaced without leaving lockfiles behind."""
    return runner_stop(workspace)


@app.command("restart", help="Restart the workspace daemon")
def daemon_restart(workspace: WorkspaceOption) -> int:
    """Stop and start the daemon in one call; used after config changes that the running daemon does not pick up live."""
    return runner_restart(workspace)


@app.command("worker", hidden=True)
def daemon_worker(workspace: WorkspaceOption) -> int:
    """Hidden entry point invoked by the daemon supervisor itself; runs the worker loop in the foreground after the supervisor has detached."""
    return runner_daemon_worker(workspace)
