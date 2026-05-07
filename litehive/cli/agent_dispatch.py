"""
Agent-role dispatch for the cold-start dispatcher.

When ``LITEHIVE_AGENT_ROLE`` is set in the environment, the running
process is an LLM subagent (planner, reviewer, swe, recovery, …)
spawned by the orchestrator — not a human operator. Subagents must
not invoke operator-only commands like ``status``, ``list``, or
``show``: those are inspection commands meant for the person running
the show, and exposing them widens the attack surface of every
prompt-injected agent run. PM-style roles still need to shape the
task they're working on (``task add|update|close``), and the
recovery role needs a small read-only diagnostic allowlist
(``pipeline journal``, ``pipeline rules``, ``task logs``); everything
else is refused with a single shared message.

This module is the sibling of ``litehive/main.py`` that owns the
agent-vs-non-agent dispatch decision so ``main.py`` can stay a thin
composition root. The policy itself lives in
``litehive.agents.command_policy``; this module only applies that
policy to raw argv and chooses whether to hand off to the full CLI.
"""

from litehive.agents.command_policy import AGENT_BLOCKED_COMMAND_MESSAGE, agent_command_is_allowed


def agent_blocked_command_message() -> str:
    """
    Single source of truth for the operator-only refusal text.

    Shared between the cold-start dispatcher and the agent CLI so
    the wording stays identical regardless of which guard fires.
    Without one helper, a ``main.py`` refusal and an ``agent_cli``
    refusal could diverge and confuse log readers grepping for the
    message.

    Called by the cold-start dispatcher in ``litehive/main.py``
    when an agent-role process tries to invoke an operator command
    and by the agent CLI when its own guards refuse a request.
    """
    return AGENT_BLOCKED_COMMAND_MESSAGE


def requests_help(argv: list[str]) -> bool:
    """
    Detect ``--help`` / ``-h`` in raw argv to route help through the full CLI.

    The fast paths produce only their command-specific output;
    rich help text comes from Typer. Routing help requests
    through the full CLI keeps that text consistent regardless of
    which command the operator asked help for.

    Called by the agent-role branch of the cold-start dispatcher
    so a subagent asking ``litehive task --help`` still gets the
    full Typer help even though the surrounding command would
    otherwise be blocked.
    """
    return any(arg in {"--help", "-h"} for arg in argv)


def dispatch_agent_role(role: str, argv: list[str]) -> int | None:
    """
    Apply the agent-role command allowlist before any sub-app loads.

    Returns an integer exit code when the dispatch is fully handled
    here (usage banner, refusal, or a successful hand-off to the
    full CLI under an allowlisted command), and ``None`` when the
    caller should fall through to the regular fast-path routing —
    that happens when the agent invokes the dedicated ``agent``
    subcommand, which the agent CLI is allowed to serve directly.

    Called by the cold-start dispatcher in ``litehive/main.py`` as
    the very first decision once ``LITEHIVE_AGENT_ROLE`` is
    detected, so an injected agent process can never sneak past
    the gate by triggering a sub-app import side effect.
    """
    if not argv:
        print("Usage: litehive agent report | litehive task [add|update|close]")
        print("\nRun 'litehive --help' for details.")
        return 0
    cmd = argv[0]
    if cmd == "report":
        print(agent_blocked_command_message())
        return 1
    route_via_root_cli = False
    if requests_help(argv):
        route_via_root_cli = True
    if cmd != "agent" and not route_via_root_cli:
        if not agent_command_is_allowed(role, argv):
            print(agent_blocked_command_message())
            return 1
        route_via_root_cli = True
    if route_via_root_cli:
        # inline: keep CLI cold start fast — the full Click tree is
        # only loaded once the gate has approved the command.
        from litehive.cli.app import main as cli_main  # noqa: PLC0415

        return cli_main()
    return None
