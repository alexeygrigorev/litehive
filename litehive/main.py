"""Lightweight CLI entrypoint with a fast status path.

This module is the process entry point and is the only place where
inline imports inside functions are routinely allowed. The reason is
cold-start latency: ``litehive`` is invoked frequently from the
shell (``litehive status``, in particular, runs in tight feedback
loops) and importing the full Click/Typer CLI and its transitive
dependencies for every invocation adds tens of milliseconds. The
``main()`` dispatcher therefore loads each sub-app only on the
branch that needs it. Code-style rule R1 in ``docs/code-style.md``
calls this out as the canonical exception.
"""

import os
import sys
from pathlib import Path

from litehive.config.workspace import normalize_workspace_root, resolve_workspace


_AGENT_ALLOWED_TASK_ROOT_COMMANDS: set[tuple[str, ...]] = {
    ("task", "add"),
    ("task", "update"),
    ("task", "close"),
}


def _agent_command_is_allowed(role: str, argv: list[str]) -> bool:
    """Return whether an agent role may invoke a non-`agent` command.

    The public agent-facing CLI is intentionally tiny:
    `litehive agent report` plus `litehive task add|update|close`.
    Recovery keeps its small diagnostic allowlist; other commands stay blocked.
    """
    if not argv:
        return False
    if tuple(argv[:2]) in _AGENT_ALLOWED_TASK_ROOT_COMMANDS and role in {"planner", "reviewer"}:
        return True
    if role != "recovery":
        return False
    return tuple(argv[:2]) in {
        ("pipeline", "journal"),
        ("pipeline", "rules"),
        ("task", "logs"),
    }


def _agent_blocked_command_message() -> str:
    """Single source of truth for the operator-only refusal text shared between ``main.py`` and the agent CLI; keeps the wording identical so logs and operators see one message regardless of which guard fires."""
    return (
        "You are not authorized to perform this command. "
        "PM agents may shape only the active task via "
        "`litehive agent update ...` or `litehive agent close ...`; "
        "operator inspection commands such as status/list/show are not available to agents."
    )


def _workspace_override_from_argv(argv: list[str]) -> Path | None:
    """Pre-parse ``--workspace`` from raw argv before Typer is loaded; needed by the fast status path which must resolve a workspace without paying the Click cold-start cost."""
    for index, arg in enumerate(argv):
        if arg == "--workspace" and index + 1 < len(argv):
            return Path(argv[index + 1])
        elif arg.startswith("--workspace="):
            return Path(arg.split("=", 1)[1])
    return None


def _requests_help(argv: list[str]) -> bool:
    """Detect ``--help``/``-h`` in raw argv so the dispatcher can route help requests through the full Typer app (which prints rich help) instead of the fast paths."""
    return any(arg in {"--help", "-h"} for arg in argv)


def dispatch_status(argv: list[str]) -> int:
    """Run the default ``litehive status`` without loading the full Click/Typer CLI.

    Lives in ``main.py`` so the common path stays cheap on cold start.
    """
    # inline: keep CLI cold start fast — these modules are heavy and only
    # needed when the user actually asks for status.
    from litehive.observability.status_diagnostics import (  # noqa: PLC0415
        render_operational_issue_lines,
        status_has_problems,
    )
    from litehive.observability.status import collect_task_pipeline_status, render_task_pipeline_status_lines  # noqa: PLC0415

    try:
        explicit_workspace = _workspace_override_from_argv(argv)
        if explicit_workspace is None:
            workspace = resolve_workspace(None, register=False)
        else:
            workspace = normalize_workspace_root(explicit_workspace, source="--workspace")
    except ValueError as exc:
        print(f"status failed: {exc}")
        return 1
    status = collect_task_pipeline_status(workspace, read_only=True)
    for line in render_task_pipeline_status_lines(status, workspace=workspace, mode="fast"):
        print(line)
    if status_has_problems(status.issues):
        print()
        for line in render_operational_issue_lines(status.issues):
            print(line)
        return 1
    return 0


def main() -> int:
    """Cold-start CLI dispatcher: route ``status``, ``agent``, ``task``, and ``pipeline`` to the smallest typer/click sub-app that handles them, falling back to the full CLI app only when needed; the goal is to keep ``litehive status`` (the hot path) sub-100ms by skipping the global Click tree."""
    argv = sys.argv[1:]

    agent_role = os.environ.get("LITEHIVE_AGENT_ROLE")
    if agent_role:
        route_via_root_cli = False
        if argv:
            cmd = argv[0]
        else:
            cmd = None
        if cmd is None:
            print("Usage: litehive agent report | litehive task [add|update|close]")
            print("\nRun 'litehive --help' for details.")
            return 0
        if cmd == "report":
            print(_agent_blocked_command_message())
            return 1
        if _requests_help(argv):
            route_via_root_cli = True
        if cmd != "agent" and not route_via_root_cli:
            if not _agent_command_is_allowed(agent_role, argv):
                print(_agent_blocked_command_message())
                return 1
            route_via_root_cli = True
        if route_via_root_cli:
            from litehive.cli.app import main as cli_main  # noqa: PLC0415

            return cli_main()

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
