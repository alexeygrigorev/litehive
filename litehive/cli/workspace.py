from pathlib import Path
from typing import Annotated

import typer
from dataclasses import dataclass

import yaml

from heru.quota.claude_quota import check_claude_quota
from heru.quota.codex_quota import check_codex_quota
from heru.quota.copilot_quota import check_copilot_quota
from heru.quota import UsageStatus
from heru.quota.zai_quota import check_zai_quota

from heru import ENGINE_CHOICES, get_engine
from litehive.attention import waiting_for_you_lines
from litehive.cli.display import format_retry_on
from litehive.cli.common import WorkspaceOption, choice
from litehive.config.engine_models import clear_persisted_engine_freeze, parse_engine_freeze_until, persist_engine_freeze_iso
from litehive.config.loading import load_config
from litehive.config.paths import config_path
from litehive.config.workspace import ensure_workspace
from litehive.daemon.registry import daemon_metadata
from litehive.observability.engine_monitoring import render_engine_monitoring_lines
from litehive.observability.status import (
    collect_recent_activity,
    find_last_completed_task,
    render_active_task_detail_lines,
    render_active_task_section,
    render_engine_health_section,
    render_full_status_header_lines,
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
    render_runtime_policy_lines,
    render_task_summary,
)
from litehive.observability.status_diagnostics import (
    collect_status_snapshot,
    render_issue_lines,
    status_has_problems,
)
from litehive.recovery.workspace_repair import repair_workspace_state
from litehive.state.records import get_task, list_tasks_state_first
from litehive.state.persist import load_state
from litehive.state.records import list_tasks
from litehive.domain.task_ops import WorkspaceConflictError, WorkspaceRepairSummary
from litehive.state.persist import load_state as load_runtime_state
from litehive.tasks.worktrees import inspect_dirty_worktree_gate

from litehive.cli.worktree_support import collect_managed_worktrees


def register_root_commands(app: typer.Typer) -> None:
    app.command("status", help="Show workspace status")(status_command)
    app.command("doctor", help="Run workspace integrity checks and optional safe fixes")(doctor_command)
    app.command("health", help="Show workspace health diagnostics")(health_command)
    app.command("engine", help="Manage engine freezes and status")(engine_command)
    app.command("repair", help="Repair stale active tasks, interrupted runs, and queue inconsistencies")(repair_command)


def _config(root):
    ensure_workspace(root)
    path = config_path(root)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return load_config(root), path, data if isinstance(data, dict) else {}


def _engine_status_line(config) -> str:
    frozen = ", ".join(f"{k}={v}" for k, v in sorted(config.engine_freeze.items())) or "-"
    engines = ", ".join(
        f"{name}(available={'yes' if c.available else 'no'}, model_override={'yes' if c.supports_model_override else 'no'}, strips_env={'yes' if c.strips_environment else 'no'})"
        for name in ENGINE_CHOICES
        for c in [get_engine(name).capabilities]
    )
    return f"default_engine: {config.default_engine} | engine_freeze: {frozen} | engines: {engines}"


def engine_command(
    workspace: WorkspaceOption = Path.cwd(),
    engine_action: Annotated[
        str, typer.Argument(click_type=choice(["freeze", "unfreeze", "status"]), help="Subcommand")
    ] = ...,
    engine_name: Annotated[
        str | None, typer.Argument(click_type=choice(ENGINE_CHOICES), help="Engine name")
    ] = None,
    until: Annotated[str | None, typer.Option(help="Freeze until this ISO date (YYYY-MM-DD)")] = None,
    reason: Annotated[str | None, typer.Option(help="Optional operator note")] = None,
) -> int:
    if engine_action == "status":
        if engine_name:
            print("engine status: does not take an engine name")
            return 1
        config, _, _ = _config(workspace)
        print(_engine_status_line(config))
        return 0
    name = engine_name
    if name not in ENGINE_CHOICES:
        print(f"engine {engine_action}: unknown engine '{name}'")
        return 1
    root = workspace.resolve()
    if engine_action == "freeze":
        normalized_until = parse_engine_freeze_until(until)
        if normalized_until is None:
            print("engine freeze: --until must be ISO date YYYY-MM-DD")
            return 1
        persist_engine_freeze_iso(root, engine_name=name, freeze_iso=normalized_until)
        print(f"engine_frozen: {name} until {normalized_until}" + (f" reason={reason}" if reason else ""))
        return 0
    if not clear_persisted_engine_freeze(root, engine_name=name):
        print(f"engine unfreeze: {name} is not frozen")
        return 1
    print(f"engine_unfrozen: {name}")
    return 0


def _safe_active_task(root, task_id):
    return get_task(root, task_id) if task_id else None


def _print_status_issues(issues) -> int:
    if not status_has_problems(issues):
        return 0
    print()
    for line in render_issue_lines(issues):
        print(line)
    return 1


def _repair_summary_lines(
    summary: WorkspaceRepairSummary,
    *,
    result_label: str,
    include_empty: bool,
    include_extended_fields: bool,
) -> list[str]:
    lines = [
        f"{result_label}: {'yes' if summary.mutated else 'no'}",
        f"stale_runner_recovered: {'yes' if summary.stale_runner_recovered else 'no'}",
    ]
    if summary.cleared_active_task_id or include_empty:
        lines.append(f"cleared_active_task_id: {summary.cleared_active_task_id or '-'}")

    items = [
        ("requeued_tasks", summary.requeued_task_ids),
        ("removed_queue_entries", summary.removed_queue_entries),
        ("deduped_queue_entries", summary.deduped_queue_entries),
    ]
    if include_extended_fields:
        items.extend(
            [
                ("restored_queue_entries", summary.restored_queue_entries),
                ("finalized_commit_tasks", summary.finalized_commit_task_ids),
                ("stale_process_tasks", summary.stale_process_task_ids),
                ("reassigned_duplicate_ids", summary.reassigned_duplicate_ids),
            ]
        )
    for label, values in items:
        if values or include_empty:
            lines.append(f"{label}: {' '.join(values) if values else '-'}")
    return lines


def _print_doctor_snapshot(root: Path) -> int:
    snapshot = collect_status_snapshot(root)
    if not snapshot.issues:
        print(f"doctor: clean workspace={root}")
        return 0
    for line in render_issue_lines(snapshot.issues):
        print(line)
    return 1


def doctor_command(
    workspace: WorkspaceOption = Path.cwd(),
    fix: Annotated[bool, typer.Option("--fix", help="Apply deterministic non-destructive fixes")] = False,
) -> int:
    ensure_workspace(workspace)
    root = workspace.resolve()
    if fix:
        try:
            summary = repair_workspace_state(root)
        except WorkspaceConflictError as exc:
            print(f"doctor failed: {exc}")
            return 1
        for line in _repair_summary_lines(
            summary,
            result_label="doctor_repaired",
            include_empty=False,
            include_extended_fields=False,
        ):
            print(line)
    return _print_doctor_snapshot(root)


def status_full(workspace, root, config, state, runner, monitoring, issues):
    active_task_id = runner.active_task_id or state.active_task_id
    for line in render_full_status_header_lines(workspace, config, state, runner):
        print(line)
    for line in waiting_for_you_lines(root):
        print(line)
    if state.queue:
        print(f"queue_head: {state.queue[0]}")
    active_task = _safe_active_task(workspace, active_task_id)
    for line in render_active_task_detail_lines(active_task, config.default_engine):
        print(line)
    for line in render_engine_monitoring_lines(monitoring):
        print(line)
    for line in render_runtime_policy_lines(config, format_retry_on(config)):
        print(line)
    tasks = list_tasks(workspace)
    if tasks:
        print()
        for task in tasks:
            for line in render_task_summary(task, active=task.id == active_task_id, root=root):
                print(line)
    return _print_status_issues(issues)


def status_command(
    workspace: WorkspaceOption = Path.cwd(),
    full: Annotated[bool, typer.Option(help="Include the full per-task status dump.")] = False,
) -> int:
    root = workspace.resolve()
    snapshot = collect_status_snapshot(root)
    config = snapshot.config
    state = snapshot.state
    runner = snapshot.runner
    monitoring = snapshot.monitoring
    if full:
        return status_full(workspace, root, config, state, runner, monitoring, snapshot.issues)

    active_task_id = runner.active_task_id or state.active_task_id
    active_task = _safe_active_task(workspace, active_task_id)
    for line in render_active_task_section(active_task, config.default_engine):
        print(line)

    all_tasks = list_tasks_state_first(workspace, state=state)
    last_done = find_last_completed_task(all_tasks)
    print()
    for line in render_last_completed_section(last_done):
        print(line)

    print()
    for line in render_queue_section(state.queue, all_tasks):
        print(line)

    print()
    for line in waiting_for_you_lines(root):
        print(line)

    print()
    for line in render_engine_health_section(monitoring):
        print(line)
    for line_text in render_engine_monitoring_lines(monitoring):
        print(line_text)

    print()
    events = collect_recent_activity(root)
    for line in render_recent_activity_section(events):
        print(line)

    return _print_status_issues(snapshot.issues)


def repair_command(workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    try:
        summary = repair_workspace_state(workspace)
    except WorkspaceConflictError as exc:
        print(f"repair failed: {exc}")
        return 1
    state = load_runtime_state(workspace)
    for line in _repair_summary_lines(
        summary,
        result_label="repaired",
        include_empty=True,
        include_extended_fields=True,
    ):
        print(line)
    print(f"active_task_id: {state.active_task_id}")
    print(f"queue_length: {len(state.queue)}")
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
    quota_health = _collect_quota_health()
    completed = sorted((task for task in tasks if task.status == "done"), key=lambda task: task.updated_at or "", reverse=True)[:3]

    print("=== Workspace Health ===")
    print(f"workspace: {root}")

    print()
    for line in render_health_active_task_lines(active_task):
        print(line)

    print()
    for line in render_health_flagged_task_lines(flagged_tasks):
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
    daemon_status, daemon_pid = _health_daemon_status(root)
    for line in render_health_daemon_lines(daemon_status, daemon_pid):
        print(line)

    print()
    for line in render_health_recent_completion_lines(completed):
        print(line)

    has_quota_problem = any(item.problem for item in quota_health)
    has_worktree_problem = dirty_report.blocks_pool
    return 1 if flagged_tasks or has_worktree_problem or has_quota_problem else 0


def _health_daemon_status(root: Path) -> tuple[str, str]:
    entry = daemon_metadata(root)
    if entry is None or entry.get("status") != "running":
        return ("stopped", "-")
    pid = entry.get("pid")
    return ("running", str(pid) if pid is not None else "-")


def _collect_quota_health() -> list[_QuotaHealth]:
    claude_status = check_claude_quota()
    codex_status = check_codex_quota()
    copilot_status = check_copilot_quota()
    zai_status = check_zai_quota()
    snapshots = {
        "claude": _quota_health(
            "claude",
            claude_status,
            reset_at=_preferred_reset_at(claude_status, include_short_term_fallback=True),
        ),
        "codex": _quota_health("codex", codex_status, reset_at=_preferred_reset_at(codex_status)),
        "copilot": _quota_health(
            "copilot",
            copilot_status,
            reset_at=_preferred_reset_at(copilot_status),
        ),
        "gemini": _unsupported_quota_health("gemini"),
        "goz": _quota_health("goz", zai_status),
        "opencode": _quota_health("opencode", zai_status),
    }
    return [snapshots[engine] for engine in ENGINE_CHOICES]


def _unsupported_quota_health(engine: str) -> _QuotaHealth:
    return _QuotaHealth(engine=engine, status="unsupported", summary="no proactive quota check")


def _preferred_reset_at(
    status: UsageStatus,
    *,
    include_short_term_fallback: bool = False,
) -> str | None:
    return status.long_term.reset_at or (status.short_term.reset_at if include_short_term_fallback else None)


def _quota_health(
    engine: str,
    status: UsageStatus,
    *,
    reset_at: str | None = None,
) -> _QuotaHealth:
    if status.error is not None:
        return _QuotaHealth(engine, "unavailable", status.error)
    summary = (
        f"short={status.short_term.percent_remaining:.1f}% remaining "
        f"long={status.long_term.percent_remaining:.1f}% remaining"
    )
    if reset_at is not None:
        summary += f" reset={reset_at}"
    return _QuotaHealth(engine, "warning" if status.limit_reached else "ok", summary, status.limit_reached)


cmd_status = status_command
cmd_doctor = doctor_command
cmd_health = health_command
cmd_engine = engine_command
cmd_repair = repair_command
