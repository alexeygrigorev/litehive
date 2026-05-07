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
    """Force the ``daemon`` group to require a subcommand instead of acting bare."""
    require_subcommand(ctx)


@app.command("run", help="Start the workspace daemon")
def daemon_run(workspace: WorkspaceOption, foreground: bool = False) -> int:
    """
    Spawn the pool daemon for a workspace.

    ``--foreground`` runs the worker loop in this process for
    development and debugging; otherwise the supervisor double-forks
    a background daemon so the operator's shell returns immediately.
    """
    if foreground:
        return runner_daemon_worker(workspace)
    return runner_start(workspace)


@app.command("status", help="Show daemon state for a workspace")
def daemon_status(workspace: StatusWorkspaceOption = None) -> int:
    """
    Report whether a daemon is running for the given workspace.

    Accepts an optional ``--workspace`` so it can be invoked from
    outside a registered repo (monitoring scripts, ad-hoc shells)
    without requiring the caller to ``cd`` into the workspace first.
    """
    try:
        if workspace is None:
            root = resolve_workspace(None)
        else:
            root = normalize_workspace_root(workspace, source="--workspace")
    except ValueError as exc:
        print(f"daemon status failed: {exc}")
        return 1
    return runner_daemon_status(root)


@app.command("stop", help="Stop the workspace daemon")
def daemon_stop(workspace: WorkspaceOption) -> int:
    """
    Signal the running daemon to exit cleanly.

    Used to replace a stuck or stale pool without leaving lockfiles
    behind; sending SIGTERM via this command lets the daemon flush
    its lockfile and writer state instead of relying on signal
    handlers from a manual ``kill``.
    """
    return runner_stop(workspace)


@app.command("restart", help="Restart the workspace daemon")
def daemon_restart(workspace: WorkspaceOption) -> int:
    """
    Stop and start the daemon in one call.

    Used after config changes that the running daemon does not pick
    up live (engine roster, pool size, sandbox policy) so operators
    can apply them without scripting the stop/start dance themselves.
    """
    return runner_restart(workspace)


@app.command("worker", hidden=True)
def daemon_worker(workspace: WorkspaceOption) -> int:
    """
    Hidden entry point invoked by the daemon supervisor process.

    Runs the worker loop in the foreground after the supervisor has
    detached from the controlling terminal; not part of the operator
    surface — the supervisor execs into this command after forking.
    """
    return runner_daemon_worker(workspace)
