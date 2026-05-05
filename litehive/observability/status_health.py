"""Health-mode renderers for ``litehive workspace health`` machine output.

Distinct from ``status_dashboard``, these helpers emit the ``key: value``
grammar the health command commits to so downstream scripts can grep the output
without re-parsing two formats.
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
    """Render the Active Task block for the ``litehive workspace health`` machine-readable output.

    The dashboard variant in ``render_active_task_section`` uses indented prose; this one
    uses the ``key: value`` grammar the health command commits to so downstream scripts can
    grep the output without re-parsing two formats.
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
    """Render the Flagged Tasks block for ``litehive workspace health``, surfacing reason and last verdict per task.

    Operators triaging a stuck workspace need both the flag reason (why the runner gave up)
    and the last verdict/summary (what the agent said before that) on one line per task,
    which is why this resolves both via the report-side helpers.
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
    """Render the Worktrees block for ``litehive workspace health``: one line per worktree with status and dirty count.

    Called by the CLI workspace-health command after it has already collected the worktree
    inventory; this helper just formats it. ``worktrees`` is typed ``Any`` to avoid a
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
    """Render the Worktree Findings block: per-finding details that explain *why* a worktree is dirty.

    Distinct from ``render_health_worktree_lines``, which is per-worktree summary; this is
    per-finding (orphan files, unowned paths, foreign locations) so the operator can see
    the actionable diff between the inventory and what's actually on disk.
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
    """Render the Engine Quotas block so operators can see which engines are throttled before a run starts.

    Called by the workspace-health CLI command. The list is allowed to be empty (no quota
    pressure observed); callers rely on the section header existing unconditionally so the
    output shape is stable for grep-based scripts.
    """
    lines = ["=== Engine Quotas ==="]
    for quota in quota_health:
        lines.append(f"quota: {quota.engine} status={quota.status} summary={quota.summary}")
    return lines


def render_health_daemon_lines(daemon_status: str, daemon_pid: str) -> list[str]:
    """Render the Daemon block for workspace health, kept as a helper so the daemon and CLI use one shape.

    Both arguments are pre-stringified by the CLI caller (``daemon_pid`` is a string so
    missing PIDs render as ``-`` without a conditional here), which is why this looks
    trivial: the policy lives in the caller and this function only owns the output shape.
    """
    return [
        "=== Daemon ===",
        f"daemon_status: {daemon_status}",
        f"daemon_pid: {daemon_pid}",
    ]


def render_health_recent_completion_lines(completed: list[TaskRecord], workspace: Workspace) -> list[str]:
    """Render Recent Completions for workspace health, summarizing each finished task with its last summary line.

    Used by the workspace-health CLI to give operators a "what just happened" snapshot
    without having to grep events. Each task is run through ``_task_last_summary_label`` so
    the verdict text matches what other status surfaces show for the same task.
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
