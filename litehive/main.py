"""
Process entry point with a cold-start-aware dispatcher.

Operators run ``litehive status`` in tight loops (and shell prompt
integrations call it for every prompt redraw), so paying tens of
milliseconds to import the full Click/Typer tree on every invocation
would hurt. ``main()`` therefore parses argv minimally and routes the
hot paths (``status``, ``agent``, ``task``, ``pipeline``) to the
smallest sub-app that can serve them, falling back to the full CLI
only when needed. This module is the canonical exception to the
"all imports at the top" rule (see ``docs/code-style.md`` Imports
section); every inline import here carries a ``# inline:`` comment.
"""

import os
import sys
from pathlib import Path

from litehive.cli.agent_dispatch import dispatch_agent_role
from litehive.config.workspace import normalize_workspace_root, resolve_workspace


def _workspace_override_from_argv(argv: list[str]) -> Path | None:
    """
    Pre-parse ``--workspace`` from raw argv before Typer is loaded.

    The fast status path must resolve the workspace without
    paying Click's cold-start cost; loading Typer just to read
    one global option would defeat the entire dispatcher
    optimization. Returns ``None`` when no override is supplied
    so the caller falls back to the workspace-discovery flow.
    """
    for index, arg in enumerate(argv):
        if arg == "--workspace" and index + 1 < len(argv):
            return Path(argv[index + 1])
        elif arg.startswith("--workspace="):
            return Path(arg.split("=", 1)[1])
    return None


def dispatch_status(argv: list[str]) -> int:
    """
    Run the default ``litehive status`` without loading the full CLI.

    Lives in ``main.py`` so the most-common operator command
    stays cheap on cold start. Imports the status renderers
    inline because they're heavy and not needed by the agent /
    task / pipeline branches.
    """
    # inline: keep CLI cold start fast — these modules are heavy and only
    # needed when the user actually asks for status.
    from litehive.observability.status_diagnostics import (  # noqa: PLC0415
        render_operational_issue_lines,
        status_has_problems,
    )
    from litehive.container import build_workspace  # noqa: PLC0415
    from litehive.observability.status import (  # noqa: PLC0415
        collect_task_pipeline_status_for_workspace,
        render_task_pipeline_status_lines,
    )

    try:
        explicit_workspace = _workspace_override_from_argv(argv)
        if explicit_workspace is None:
            workspace = resolve_workspace(None)
        else:
            workspace = normalize_workspace_root(explicit_workspace, source="--workspace")
    except ValueError as exc:
        print(f"status failed: {exc}")
        return 1
    workspace_obj = build_workspace(workspace)
    status = collect_task_pipeline_status_for_workspace(workspace_obj, read_only=True)
    for line in render_task_pipeline_status_lines(status, workspace=workspace_obj.root, mode="summary"):
        print(line)
    if status_has_problems(status.issues):
        print()
        for line in render_operational_issue_lines(status.issues):
            print(line)
        return 1
    return 0


def main() -> int:
    """
    Cold-start CLI dispatcher.

    Routes ``status``, ``agent``, ``task``, and ``pipeline`` to
    the smallest Typer/Click sub-app that can handle them and
    falls back to the full CLI only when needed. Keeps
    ``litehive status`` (the hot path) sub-100ms by skipping the
    global Click tree on the common case; agent-role invocations
    additionally enforce the operator/agent command allowlist
    before any sub-app loads.
    """
    argv = sys.argv[1:]

    agent_role = os.environ.get("LITEHIVE_AGENT_ROLE")
    if agent_role:
        agent_exit_code = dispatch_agent_role(agent_role, argv)
        if agent_exit_code is not None:
            return agent_exit_code

    if argv and argv[0] == "status" and "--full" not in argv and "--fast" not in argv:
        return dispatch_status(argv[1:])

    if argv and argv[0] == "agent":
        import click  # noqa: PLC0415
        from litehive.cli.agent_cli import agent_app  # noqa: PLC0415

        sys.argv = [sys.argv[0], *argv[1:]]
        try:
            result = agent_app(standalone_mode=False)
        except click.exceptions.Exit as exc:
            return exc.exit_code
        except click.ClickException as exc:
            exc.show()
            return exc.exit_code
        except click.Abort:
            return 1
        if result is None:
            return 0
        return int(result)

    if argv and argv[0] == "task":
        import click  # noqa: PLC0415
        from litehive.cli.task_cli import app as task_app  # noqa: PLC0415

        sys.argv = [sys.argv[0], *argv[1:]]
        try:
            result = task_app(standalone_mode=False)
        except click.exceptions.Exit as exc:
            return exc.exit_code
        except click.ClickException as exc:
            exc.show()
            return exc.exit_code
        except click.Abort:
            return 1
        if result is None:
            return 0
        return int(result)

    if argv and argv[0] == "pipeline":
        import click  # noqa: PLC0415
        from litehive.cli.pipeline_cli import app as pipeline_app  # noqa: PLC0415

        sys.argv = [sys.argv[0], *argv[1:]]
        try:
            result = pipeline_app(standalone_mode=False)
        except click.exceptions.Exit as exc:
            return exc.exit_code
        except click.ClickException as exc:
            exc.show()
            return exc.exit_code
        except click.Abort:
            return 1
        if result is None:
            return 0
        return int(result)

    from litehive.cli.app import main as cli_main  # noqa: PLC0415

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
