"""Render helpers that turn a list of `StatusIssue`s into the issue blocks shown by `litehive status`/`health`."""

from litehive.observability.status_types import StatusIssue


def status_has_problems(issues: list[StatusIssue]) -> bool:
    """Decide whether the status command should print an issues block and exit non-zero."""
    return any(issue.severity in {"WARN", "ERROR"} for issue in issues)


def render_health_summary(issues: list[StatusIssue]) -> str:
    """Format the trailing `health: N broken, M warning` line that closes both `litehive status` and
    `litehive health` issue blocks so operators see severity counts at a glance."""
    broken = sum(1 for issue in issues if issue.severity == "ERROR")
    warning = sum(1 for issue in issues if issue.severity == "WARN")
    return f"health: {broken} broken, {warning} warning"


def render_issue_lines(issues: list[StatusIssue]) -> list[str]:
    """Render the verbose issue block (full message + remediation) shown by `litehive health`."""
    if not status_has_problems(issues):
        return []
    return [*(issue.render() for issue in issues), render_health_summary(issues)]


def render_operational_issue_lines(issues: list[StatusIssue]) -> list[str]:
    """Render the terse issue block used by the default `litehive status`, with remediation hints stripped."""
    if not status_has_problems(issues):
        return []
    return [
        *(f"{issue.key}: {_operational_issue_message(issue.message)}" for issue in issues),
        render_health_summary(issues),
    ]


def _operational_issue_message(message: str) -> str:
    """Trim the ` — remediation…` tail off a status message so default `status` output stays one-line per issue."""
    return message.split(" — ", 1)[0]
