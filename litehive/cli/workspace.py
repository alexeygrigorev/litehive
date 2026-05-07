from pathlib import Path
from typing import Annotated, cast
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
from litehive.config.workspace import create_workspace
from litehive.container import build_workspace
from litehive.daemon.registry import daemon_metadata
from litehive.domain.common import TaskStatus
from litehive.observability.status import (
    collect_recent_activity,
    collect_task_pipeline_status_for_workspace,
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
from litehive.state.records import list_tasks_state_first
from litehive.state.persist import load_state
from litehive.domain.task_ops import WorkspaceConflictError, WorkspaceRepairSummary
from litehive.worktree.cleanup import collect_managed_worktrees_for_workspace
from litehive.worktree.inspection import inspect_dirty_worktree_gate
from litehive.workspace import Workspace


def register_root_commands(app: typer.Typer) -> None:
    """
    Wire the workspace-level commands onto the root Typer app.

    Adds ``status``, ``health``, ``engine``, and ``repair`` from
    a single registration call so importing this module has no
    Typer side effects. Called once during CLI bootstrap from
    ``litehive.cli.app``.
    """
    app.command("status", help="Show workspace status")(status_command)
    app.command("health", help="Show workspace health diagnostics")(health_command)
    app.command("engine", help="Manage engine freezes and status")(engine_command)
    app.command("repair", help="Repair stale active tasks, interrupted runs, and queue inconsistencies")(repair_command)


def print_status_issues(issues) -> int:
    """
    Emit the diagnostics block at the bottom of ``status``.

    Returns a non-zero exit code when any issue is severe enough
    to fail the command so monitoring scripts can wire the exit
    code into alerting. Tests monkey-patch this to silence
    diagnostics noise without disabling the broader status path.
    """
    if not status_has_problems(issues):
        return 0
    print()
    for line in render_issue_lines(issues):
        print(line)
    return 1


def repair_summary_lines(
    summary: WorkspaceRepairSummary,
    result_label: str,
    include_empty: bool,
    include_extended_fields: bool,
) -> list[str]:
    """
    Render the workspace-repair summary as printable lines.

    ``result_label`` lets the same formatter serve both the
    explicit ``repair`` command (label ``"repaired"``) and the
    implicit cleanup that runs from other commands; without it,
    each caller would have to translate the summary to text on
    its own. ``include_empty`` keeps zero-element fields in the
    output so the layout stays uniform.
    """
    del include_extended_fields
    if summary.mutated:
        mutated_label = "yes"
    else:
        mutated_label = "no"
    if summary.stale_runner_recovered:
        stale_runner_label = "yes"
    else:
        stale_runner_label = "no"
    lines = [
        f"{result_label}: {mutated_label}",
        f"stale_runner_recovered: {stale_runner_label}",
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
            if values:
                values_label = " ".join(values)
            else:
                values_label = "-"
            lines.append(f"{label}: {values_label}")
    return lines


def status_command(
    workspace: WorkspaceOption = Path.cwd(),
    full: Annotated[bool, typer.Option(help="Include the full per-task status dump.")] = False,
) -> int:
    """
    Operator-facing workspace overview.

    Renders the active task, queue, recent activity, and engine
    availability sections, then surfaces any diagnostics at the
    bottom and returns non-zero when those diagnostics are
    severe. ``--full`` adds the per-task pipeline dump for
    debugging — kept off by default so the common operator view
    fits one screen.
    """
    ws = build_workspace(workspace)
    root = ws.root
    status = collect_task_pipeline_status_for_workspace(ws, diagnostics=full)
    if full:
        for line in render_task_pipeline_status_lines(
            status,
            workspace=root,
            mode="detailed",
            retry_on_label=format_retry_on(status.config),
        ):
            print(line)
        tasks = ws.list_tasks(strict=False)
        if tasks:
            print()
            for task in tasks:
                for line in render_task_summary(task, active=task.id == status.active_task_id, workspace=ws):
                    print(line)
        return print_status_issues(status.issues)

    for line in render_active_task_section(status.active_task, status.config.default_engine):
        print(line)

    all_tasks = list_tasks_state_first(workspace, state=status.state)
    last_done = find_last_completed_task(all_tasks)
    print()
    for line in render_last_completed_section(last_done, ws):
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
    events = collect_recent_activity(ws)
    for line in render_recent_activity_section(events):
        print(line)

    return print_status_issues(status.issues)


def repair_command(workspace: WorkspaceOption = Path.cwd()) -> int:
    """
    Reconcile workspace state.

    Clears stale active-task pointers, requeues interrupted
    tasks, and normalizes terminal entries. The explicit
    operator-facing ``repair`` command for when the daemon is
    not picking up where it left off — distinct from the
    automatic cleanup that runs from ``queue`` and ``run`` so
    the operator can also force a full reconciliation pass.
    """
    create_workspace(workspace)
    workspace_obj = build_workspace(workspace)
    start_time = time.perf_counter()
    try:
        summary = repair_workspace_state(workspace_obj)
    except WorkspaceConflictError as exc:
        print(f"repair failed: {exc}")
        return 1
    end_time = time.perf_counter()
    duration_ms = (end_time - start_time) * 1000

    state = load_state(workspace)
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
    """
    Render the workspace-health report.

    Covers active/flagged tasks, worktrees, engine quotas, daemon
    status, and recent completions. Exits non-zero when something
    needs operator attention so external monitoring can wire the
    exit code into alerting; that is the difference between this
    command and ``status``, which is the broader interactive view.
    """
    create_workspace(workspace)
    ws = build_workspace(workspace)
    root = ws.root
    state = load_state(root)
    tasks = list_tasks_state_first(root, state=state, include_runtime=True)
    if state.active_task_id:
        active_task = ws.get_task(state.active_task_id)
    else:
        active_task = None
    flagged_tasks = [task for task in tasks if task.status == TaskStatus.FLAGGED]
    worktrees = collect_managed_worktrees_for_workspace(ws)
    dirty_report = inspect_dirty_worktree_gate(ws)
    quota_health = collect_quota_health()
    completed = sorted(
        (task for task in tasks if task.status == TaskStatus.DONE),
        key=lambda task: task.updated_at or "",
        reverse=True,
    )[:3]

    print("=== Workspace Health ===")
    print(f"workspace: {root}")

    print()
    for line in render_health_active_task_lines(active_task):
        print(line)

    print()
    for line in render_health_flagged_task_lines(flagged_tasks, workspace=ws):
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
    daemon_status, daemon_pid = health_daemon_status_for_workspace(ws)
    for line in render_health_daemon_lines(daemon_status, daemon_pid):
        print(line)

    print()
    for line in render_health_recent_completion_lines(completed, workspace=ws):
        print(line)

    has_quota_problem = any(item.problem for item in quota_health)
    has_worktree_problem = dirty_report.blocks_pool
    if flagged_tasks or has_worktree_problem or has_quota_problem:
        return 1
    return 0


def health_daemon_status(root: Path) -> tuple[str, str]:
    """
    Path-based compatibility wrapper for daemon health status.
    """
    return health_daemon_status_for_workspace(build_workspace(root))


def health_daemon_status_for_workspace(workspace: Workspace) -> tuple[str, str]:
    """
    Return a ``(status, pid)`` pair for the workspace daemon.

    Renders the same shape the health command uses so tests can
    assert the running/stopped surface without invoking the full
    health report. Returns ``("stopped", "-")`` when no daemon
    record is present so the caller can render the row without
    branching.
    """
    entry = daemon_metadata(workspace.root)
    if entry is None or entry.status != "running":
        return ("stopped", "-")
    if entry.pid is not None:
        pid_label = str(entry.pid)
    else:
        pid_label = "-"
    return ("running", pid_label)


def collect_quota_health() -> list[_QuotaHealth]:
    """
    Probe each engine's quota provider once and return health rows.

    Returned in :data:`ENGINE_CHOICES` order so ``health`` always
    renders the engines in a stable lineup — operators rely on
    that ordering when scanning successive runs. Z.ai's probe
    covers both ``goz`` and ``opencode`` because they share a
    backend; ``gemini`` has no probe surface.
    """
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
    """
    Build a placeholder row for engines without a quota check.

    Lets the health command render Gemini (and any future
    no-probe engine) as ``unsupported`` rather than as a hard
    failure; an unsupported engine is not a problem, the
    operator just cannot prove its quota state remotely.
    """
    return _QuotaHealth(engine=engine, status="unsupported", summary="no proactive quota check")


def quota_health(
    engine: str,
    status: UsageStatus | object,
) -> _QuotaHealth:
    """
    Translate a heru ``UsageStatus`` into a renderable ``_QuotaHealth``.

    Tolerates engines whose provider returns a different schema
    by flagging them as ``unavailable`` with the ``problem`` bit
    set so operators see them as failures rather than silent
    gaps. ``limit_reached`` becomes the ``warning`` status so the
    health row can be acted on without parsing the summary text.
    """
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
    reset_at = preferred_reset_at(cast("UsageStatus", status), include_short_term_fallback=True)
    summary = f"hours remaining={float(short_remaining):.1f}% weeks remaining={float(long_remaining):.1f}%"
    if reset_at is not None:
        summary += f" reset={reset_at}"
    limit_reached = bool(getattr(status, "limit_reached", False))
    if limit_reached:
        quota_status_label = "warning"
    else:
        quota_status_label = "ok"
    return _QuotaHealth(engine, quota_status_label, summary, limit_reached)
