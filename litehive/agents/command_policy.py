"""
Agent-facing command policy.

The cold-start CLI dispatcher needs this policy before loading the
full Typer tree, but the rules are agent authorization behavior, not
CLI rendering. Keep this module pure so the dispatcher can import it
cheaply and tests can exercise the policy without booting Click.
"""

AGENT_BLOCKED_COMMAND_MESSAGE = (
    "You are not authorized to perform this command. "
    "PM agents may shape only the active task via "
    "`litehive agent update ...` or `litehive agent close ...`; "
    "operator inspection commands such as status/list/show are not available to agents."
)

_PM_AGENT_ROLES = {"planner", "reviewer"}
_PM_AGENT_ALLOWED_TASK_COMMANDS: set[tuple[str, str]] = {
    ("task", "add"),
    ("task", "update"),
    ("task", "close"),
}
_RECOVERY_AGENT_ALLOWED_DIAGNOSTIC_COMMANDS: set[tuple[str, str]] = {
    ("pipeline", "journal"),
    ("pipeline", "rules"),
    ("task", "logs"),
}


def agent_command_is_allowed(role: str, argv: list[str]) -> bool:
    """
    Return whether an agent role may invoke a non-agent command.
    """
    if not argv:
        return False
    command_pair = tuple(argv[:2])
    if command_pair in _PM_AGENT_ALLOWED_TASK_COMMANDS and role in _PM_AGENT_ROLES:
        return True
    if role != "recovery":
        return False
    return command_pair in _RECOVERY_AGENT_ALLOWED_DIAGNOSTIC_COMMANDS
