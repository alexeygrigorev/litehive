from pathlib import Path
from typing import Annotated
import time

import typer
from dataclasses import dataclass

from heru import ENGINE_CHOICES
from heru.quota import (
    UsageStatus,
    check_claude_quota,
    check_codex_quota,
    check_copilot_quota,
    check_zai_quota,
    preferred_reset_at,
)
from litehive.cli.engine import engine_command
from litehive.cli.display import format_retry_on
from litehive.cli.common import WorkspaceOption
from litehive.config.workspace import ensure_workspace
from litehive.daemon.registry import daemon_metadata
from litehive.observability.status import (
    collect_task_pipeline_status,
    collect_recent_activity,
    find_last_completed_task,
    render_active_task_section,
    render_engine_availability_lines,
    render_health_active_task_lines,
    render_health_daemon_lines,
    render_health_flagged_task_lines,
    render_health_quota_lines,
    render_health_recent_completion_lines,
    render_health_worktree_finding_lines,
    render_health_worktree_lines,
    render_last_completed_section,
    render_queue_section,
    render_recent_activity_section,
    render_task_pipeline_status_lines,
    render_task_summary,
)
from litehive.observability.status_diagnostics import (
    render_issue_lines,
    status_has_problems,
)
from litehive.recovery.workspace_repair import repair_workspace_state
from litehive.state.records import get_task, list_tasks_state_first
from litehive.state.persist import load_state
from litehive.state.records import list_tasks
from litehive.domain.task_ops import WorkspaceConflictError, WorkspaceRepairSummary
from litehive.state.persist import load_state as load_runtime_state
from litehive.worktree_cleanup import collect_managed_worktrees
from litehive.worktree_inspection import inspect_dirty_worktree_gate


def register_root_commands(app: typer.Typer) -> None:
    app.command("status", help="Show workspace status")(status_command)
    app.command("health", help="Show workspace health diagnostics")(health_command)
    app.command("engine", help="Manage engine freezes and status")(engine_command)
    app.command("repair", help="Repair stale active tasks, interrupted runs, and queue inconsistencies")(repair_command)


def print_status_issues(issues) -> int:
    if not status_has_problems(issues):
        return 0
    print()
    for line in render_issue_lines(issues):
        print(line)
    return 1


def repair_summary_lines(
    summary: WorkspaceRepairSummary,
    *,
    result_label: str,
    include_empty: bool,
    include_extended_fields: bool,
) -> list[str]:
    del include_extended_fields
    lines = [
        f"{result_label}: {'yes' if summary.mutated else 'no'}",
        f"stale_runner_recovered: {'yes' if summary.stale_runner_recovered else 'no'}",
    ]
    if summary.cleared_active_task_id or include_empty:
        lines.append(f"cleared_active_task_id: {summary.cleared_active_task_id or '-'}")

    items = [
        ("requeued_tasks", summary.requeued_task_ids),
        ("stale_process_tasks", summary.stale_process_task_ids),
        ("normalized_terminal_tasks", summary.terminal_task_ids),
    ]
    for label, values in items:
        if values or include_empty:
            lines.append(f"{label}: {' '.join(values) if values else '-'}")
    return lines


def status_command(
    workspace: WorkspaceOption = Path.cwd(),
    full: Annotated[bool, typer.Option(help="Include the full per-task status dump.")] = False,
) -> int:
    root = workspace.resolve()
    status = collect_task_pipeline_status(root, diagnostics=full)
    if full:
        for line in render_task_pipeline_status_lines(
            status,
            workspace=workspace,
            mode="full",
            retry_on_label=format_retry_on(status.config),
        ):
            print(line)
        tasks = list_tasks(workspace, strict=False)
        if tasks:
            print()
            for task in tasks:
                for line in render_task_summary(task, active=task.id == status.active_task_id, root=root):
                    print(line)
        return print_status_issues(status.issues)

    for line in render_active_task_section(status.active_task, status.config.default_engine):
        print(line)

    all_tasks = list_tasks_state_first(workspace, state=status.state)
    last_done = find_last_completed_task(all_tasks)
    print()
    for line in render_last_completed_section(last_done):
        print(line)

    print()
    for line in render_queue_section(status.state.queue, all_tasks):
        print(line)

    print()
    for line in status.waiting_lines:
        print(line)

    print()
    print("=== Engine Availability ===")
    availability_lines = render_engine_availability_lines(status.config, status.monitoring)
    if availability_lines:
        for line in availability_lines:
            print(line)
    else:
        print("  (no configured engines)")

    print()
    events = collect_recent_activity(root)
    for line in render_recent_activity_section(events):
        print(line)

    return print_status_issues(status.issues)


def repair_command(workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    start_time = time.perf_counter()
    try:
        summary = repair_workspace_state(workspace)
    except WorkspaceConflictError as exc:
        print(f"repair failed: {exc}")
        return 1
    end_time = time.perf_counter()
    duration_ms = (end_time - start_time) * 1000

    state = load_runtime_state(workspace)
    for line in repair_summary_lines(
        summary,
        result_label="repaired",
        include_empty=True,
        include_extended_fields=True,
    ):
        print(line)
    print(f"active_task_id: {state.active_task_id}")
    print(f"queue_length: {len(state.queue)}")

    # Add explicit clean-run message with timing
    if not summary.repaired:
        print(f"repair completed clean run in {duration_ms:.1f}ms - no inconsistencies found")
    else:
        print(f"repair completed in {duration_ms:.1f}ms")

    return 0


@dataclass(slots=True)
class _QuotaHealth:
    engine: str
    status: str
    summary: str
    problem: bool = False


def health_command(workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    root = workspace.resolve()
    state = load_state(root)
    tasks = list_tasks_state_first(root, state=state, include_runtime=True)
    active_task = get_task(root, state.active_task_id) if state.active_task_id else None
    flagged_tasks = [task for task in tasks if task.status == "flagged"]
    worktrees = collect_managed_worktrees(root)
    dirty_report = inspect_dirty_worktree_gate(root)
    quota_health = collect_quota_health()
    completed = sorted(
        (task for task in tasks if task.status == "done"), key=lambda task: task.updated_at or "", reverse=True
    )[:3]

    print("=== Workspace Health ===")
    print(f"workspace: {root}")

    print()
    for line in render_health_active_task_lines(active_task):
        print(line)

    print()
    for line in render_health_flagged_task_lines(flagged_tasks, root=root):
        print(line)

    print()
    for line in render_health_worktree_lines(worktrees):
        print(line)

    print()
    for line in render_health_worktree_finding_lines(dirty_report):
        print(line)

    print()
    for line in render_health_quota_lines(quota_health):
        print(line)

    print()
    daemon_status, daemon_pid = health_daemon_status(root)
    for line in render_health_daemon_lines(daemon_status, daemon_pid):
        print(line)

    print()
    for line in render_health_recent_completion_lines(completed, root=root):
        print(line)

    has_quota_problem = any(item.problem for item in quota_health)
    has_worktree_problem = dirty_report.blocks_pool
    return 1 if flagged_tasks or has_worktree_problem or has_quota_problem else 0


def health_daemon_status(root: Path) -> tuple[str, str]:
    entry = daemon_metadata(root)
    if entry is None or entry.get("status") != "running":
        return ("stopped", "-")
    pid = entry.get("pid")
    return ("running", str(pid) if pid is not None else "-")


def collect_quota_health() -> list[_QuotaHealth]:
    claude_status = check_claude_quota()
    codex_status = check_codex_quota()
    copilot_status = check_copilot_quota()
    zai_status = check_zai_quota()
    snapshots = {
        "claude": quota_health("claude", claude_status),
        "codex": quota_health("codex", codex_status),
        "copilot": quota_health("copilot", copilot_status),
        "gemini": _unsupported_quota_health("gemini"),
        "goz": quota_health("goz", zai_status),
        "opencode": quota_health("opencode", zai_status),
    }
    return [snapshots[engine] for engine in ENGINE_CHOICES]


def _unsupported_quota_health(engine: str) -> _QuotaHealth:
    return _QuotaHealth(engine=engine, status="unsupported", summary="no proactive quota check")


def quota_health(
    engine: str,
    status: UsageStatus | object,
) -> _QuotaHealth:
    error = getattr(status, "error", None)
    if error is not None:
        return _QuotaHealth(engine, "unavailable", error)
    short_term = getattr(status, "short_term", None)
    long_term = getattr(status, "long_term", None)
    if short_term is None or long_term is None:
        return _QuotaHealth(engine, "unavailable", "unsupported usage shape", problem=True)
    short_remaining = getattr(short_term, "percent_remaining", None)
    long_remaining = getattr(long_term, "percent_remaining", None)
    if short_remaining is None or long_remaining is None:
        return _QuotaHealth(engine, "unavailable", "unsupported usage shape", problem=True)
    reset_at = preferred_reset_at(status, include_short_term_fallback=True)
    summary = f"hours remaining={float(short_remaining):.1f}% weeks remaining={float(long_remaining):.1f}%"
    if reset_at is not None:
        summary += f" reset={reset_at}"
    limit_reached = bool(getattr(status, "limit_reached", False))
    return _QuotaHealth(engine, "warning" if limit_reached else "ok", summary, limit_reached)


cmd_status = status_command
cmd_health = health_command
cmd_repair = repair_command
