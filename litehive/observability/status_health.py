"""
Health-mode renderers for ``litehive workspace health``.

Distinct from :mod:`litehive.observability.status_dashboard`,
these helpers emit the ``key: value`` grammar the health
command commits to so downstream scripts can grep the output
without re-parsing the prose form. The two surfaces share the
same per-task helpers but differ in shape because health output
is contract for monitoring; dashboard output is for humans.
"""

from typing import Any

from litehive.domain.task import TaskRecord
from litehive.observability.status_summary import (
    _task_last_summary_label,
    _task_last_verdict_label,
    _task_stage_label,
)
from litehive.workspace import Workspace


def render_health_active_task_lines(task: TaskRecord | None) -> list[str]:
    """
    Render the Active Task block for ``litehive workspace health``.

    The dashboard variant in
    :func:`render_active_task_section` uses indented prose; this
    one uses the ``key: value`` grammar so downstream monitoring
    scripts can grep one consistent format.
    """
    lines = ["=== Active Task ==="]
    if task is None:
        lines.append("active_task: none")
        return lines
    lines.append(
        f"active_task: {task.id} [{task.status}/{task.pipeline_status}] "
        f"stage={_task_stage_label(task)} title={task.title}"
    )
    return lines


def render_health_flagged_task_lines(flagged_tasks: list[TaskRecord], workspace: Workspace) -> list[str]:
    """
    Render the Flagged Tasks block for workspace health.

    Operators triaging a stuck workspace need both the flag
    reason (why the runner gave up) and the last verdict/summary
    (what the agent said before that) on one line per task, so
    this resolves both via the report-side helpers. Without
    summary on the same line, an operator would be one extra
    ``task evidence`` away from acting.
    """
    lines = ["=== Flagged Tasks ===", f"flagged_count: {len(flagged_tasks)}"]
    if not flagged_tasks:
        lines.append("flagged: none")
        return lines
    for task in flagged_tasks:
        lines.append(
            f"flagged: {task.id} stage={_task_stage_label(task)} "
            f"reason={task.flag_reason or 'unknown'} "
            f"last_verdict={_task_last_verdict_label(task, workspace=workspace)} "
            f"summary={_task_last_summary_label(task, workspace=workspace)}"
        )
    return lines


def render_health_worktree_lines(worktrees: list[Any]) -> list[str]:
    """
    Render the Worktrees block for workspace health.

    One line per worktree with status, dirty change count, and
    on-disk path so the operator can spot a leaking worktree
    without an extra ``worktree ls``. Called by the CLI
    workspace-health command after it has collected the
    inventory. ``worktrees`` is typed ``Any`` to avoid a
    cross-module import cycle with the worktree package.
    """
    lines = ["=== Worktrees ===", f"worktree_count: {len(worktrees)}"]
    if not worktrees:
        lines.append("worktree: none")
        return lines
    for item in worktrees:
        if item.active:
            active_label = "yes"
        else:
            active_label = "no"
        lines.append(
            f"worktree: {item.task_id} status={item.status} changes={item.change_count} "
            f"active={active_label} path={item.worktree_rel}"
        )
    return lines


def render_health_worktree_finding_lines(report: Any) -> list[str]:
    """
    Render the Worktree Findings block.

    Per-finding details (orphan files, unowned paths, foreign
    locations) explaining *why* a worktree is dirty. Distinct
    from :func:`render_health_worktree_lines`, which is the
    per-worktree summary — this is the actionable diff between
    the inventory and what is actually on disk.
    """
    lines = ["=== Worktree Findings ==="]
    if report.is_clean:
        lines.append("worktree_findings: clean")
        return lines
    for finding in report.findings:
        details = [f"location={finding.location_kind}", f"ownership={finding.ownership}"]
        if finding.task_id:
            details.append(f"task_id={finding.task_id}")
        if finding.worktree_path:
            details.append(f"path={finding.worktree_path}")
        if finding.dirty_paths:
            dirty_paths_label = ",".join(finding.dirty_paths)
        else:
            dirty_paths_label = "-"
        details.append("dirty_paths=" + dirty_paths_label)
        lines.append("finding: " + " ".join(details))
    return lines


def render_health_quota_lines(quota_health: list[Any]) -> list[str]:
    """
    Render the Engine Quotas block.

    Lets operators see which engines are throttled before a run
    starts. Called by the workspace-health CLI command. The
    section header is rendered unconditionally even when the
    list is empty so the output shape stays stable for
    grep-based monitoring scripts.
    """
    lines = ["=== Engine Quotas ==="]
    for quota in quota_health:
        lines.append(f"quota: {quota.engine} status={quota.status} summary={quota.summary}")
    return lines


def render_health_daemon_lines(daemon_status: str, daemon_pid: str) -> list[str]:
    """
    Render the Daemon block for workspace health.

    Kept as a helper so the daemon and CLI use one block shape.
    Both arguments are pre-stringified by the CLI caller —
    ``daemon_pid`` is a string so missing PIDs render as ``-``
    without a conditional here. The policy lives in the caller
    and this function only owns the output layout.
    """
    return [
        "=== Daemon ===",
        f"daemon_status: {daemon_status}",
        f"daemon_pid: {daemon_pid}",
    ]


def render_health_recent_completion_lines(completed: list[TaskRecord], workspace: Workspace) -> list[str]:
    """
    Render Recent Completions for workspace health.

    Summarizes each finished task with its last summary line
    so operators get a "what just happened" snapshot without
    grepping events. Each task is run through
    :func:`_task_last_summary_label` so the verdict text
    matches what other status surfaces show for the same task.
    """
    lines = ["=== Recent Completions ==="]
    if not completed:
        lines.append("completed: none")
        return lines
    for task in completed:
        lines.append(
            f"completed: {task.id} title={task.title} when={task.updated_at or '-'} "
            f"summary={_task_last_summary_label(task, workspace=workspace)}"
        )
    return lines
