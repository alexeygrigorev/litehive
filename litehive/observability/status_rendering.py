"""
Render helpers for the status-issue blocks.

Turn a list of :class:`StatusIssue` instances into the text
blocks ``litehive status`` and ``litehive health`` print at the
bottom of their output. Two shapes are supported: the verbose
form with remediation tails and the operational form trimmed
back to one line per issue.
"""

from litehive.observability.status_types import StatusIssue


def status_has_problems(issues: list[StatusIssue]) -> bool:
    """
    Decide whether status output should print issues and exit non-zero.

    Treats any ``WARN`` or ``ERROR`` issue as a problem; pure
    informational notes do not trigger the issue block. The
    boolean return is used by the CLI to gate both the printout
    and the exit code in one decision.
    """
    return any(issue.severity in {"WARN", "ERROR"} for issue in issues)


def render_health_summary(issues: list[StatusIssue]) -> str:
    """
    Format the trailing ``health: N broken, M warning`` summary line.

    Closes both ``litehive status`` and ``litehive health``
    issue blocks so operators see severity counts at a glance
    without having to scan the per-issue lines. Always renders
    the same shape so monitoring scripts can grep one format.
    """
    broken = sum(1 for issue in issues if issue.severity == "ERROR")
    warning = sum(1 for issue in issues if issue.severity == "WARN")
    return f"health: {broken} broken, {warning} warning"


def render_issue_lines(issues: list[StatusIssue]) -> list[str]:
    """
    Render the verbose issue block used by ``litehive health``.

    Each issue carries its full message plus remediation tail
    so the operator sees both the diagnosis and the
    recommended action. Returns an empty list when no issue
    rises above the problem threshold.
    """
    if not status_has_problems(issues):
        return []
    return [*(issue.render() for issue in issues), render_health_summary(issues)]


def render_operational_issue_lines(issues: list[StatusIssue]) -> list[str]:
    """
    Render the terse issue block used by default ``litehive status``.

    Trims the ``— remediation…`` tail so each issue fits one
    line; status output is for routine reads where the
    operator only wants to know there *is* a problem, not the
    full triage steps. ``litehive health`` is the verbose
    counterpart for triage proper.
    """
    if not status_has_problems(issues):
        return []
    issue_lines = _terse_operational_issue_lines(issues)
    return [
        *issue_lines,
        render_health_summary(issues),
    ]


def _terse_operational_issue_lines(issues: list) -> list[str]:
    """
    Render one ``key: short-message`` line per operational issue.

    Drops the verbose remediation tail through
    :func:`_operational_issue_message` so the default
    ``status`` block stays scannable; the long form is reserved
    for ``litehive health``. Caller:
    :func:`render_operational_issue_block`.
    """
    lines: list[str] = []
    for issue in issues:
        short_message = _operational_issue_message(issue.message)
        lines.append(f"{issue.key}: {short_message}")
    return lines


def _operational_issue_message(message: str) -> str:
    """
    Strip the ``" — remediation…"`` tail from a status message.

    Keeps the default ``status`` output one line per issue so
    the diagnostics block does not balloon and crowd out the
    workspace overview. The tail is intentionally still
    present in the underlying message for the verbose health
    renderer.
    """
    return message.split(" — ", 1)[0]
