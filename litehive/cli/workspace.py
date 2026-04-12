from pathlib import Path
from typing import Annotated

import typer
from dataclasses import dataclass
from datetime import UTC, datetime

import yaml

from heru.quota.claude_quota import check_claude_quota
from heru.quota.codex_quota import check_codex_quota
from heru.quota.copilot_quota import check_copilot_quota
from heru.quota.zai_quota import check_zai_quota

from litehive.agents import ENGINE_CHOICES, get_engine
from litehive.attention import waiting_for_you_lines
from litehive.cli.display import format_retry_on
from litehive.cli.common import WorkspaceOption, choice
from litehive.config import config_path, ensure_workspace, load_config
from litehive.config.engine_models import active_engine_freezes
from litehive.daemon import daemon_status_lines
from litehive.observability import (
    collect_recent_activity,
    find_last_completed_task,
    render_active_task_section,
    render_engine_health_section,
    render_engine_monitoring_lines,
    render_last_completed_section,
    render_queue_section,
    render_recent_activity_section,
    render_task_summary,
)
from litehive.observability.status_diagnostics import (
    collect_status_snapshot,
    render_health_summary,
    status_has_problems,
)
from litehive.recovery import (
    repair_workspace_state,
)
from litehive.tasks import list_tasks_state_first, load_state, require_task
from litehive.tasks.crud import list_tasks
from litehive.tasks.models import WorkspaceConflictError
from litehive.tasks.persistence import load_state as load_runtime_state
from litehive.workspace.worktree_inspection import inspect_dirty_worktree_gate

from litehive.cli.worktree import collect_managed_worktrees


def register_root_commands(app: typer.Typer) -> None:
    app.command("status", help="Show workspace status")(status_command)
    app.command("health", help="Show workspace health diagnostics")(health_command)
    app.command("engine", help="Manage engine freezes and status")(engine_command)
    app.command("repair", help="Repair stale active tasks, interrupted runs, and queue inconsistencies")(repair_command)


def _config(root):
    ensure_workspace(root)
    path = config_path(root)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return load_config(root), path, data if isinstance(data, dict) else {}


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
        frozen = ", ".join(f"{k}={v}" for k, v in sorted(config.engine_freeze.items())) or "-"
        engines = ", ".join(
            f"{name}(available={'yes' if c.available else 'no'}, model_override={'yes' if c.supports_model_override else 'no'}, strips_env={'yes' if c.strips_environment else 'no'})"
            for name in ENGINE_CHOICES
            for c in [get_engine(name).capabilities]
        )
        print(f"default_engine: {config.default_engine} | engine_freeze: {frozen} | engines: {engines}")
        return 0
    name = engine_name
    if name not in ENGINE_CHOICES:
        print(f"engine {engine_action}: unknown engine '{name}'")
        return 1
    _, path, raw = _config(workspace)
    frozen = raw.get("engine_freeze") if isinstance(raw.get("engine_freeze"), dict) else {}
    if engine_action == "freeze":
        try:
            until = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            print("engine freeze: --until must be ISO date YYYY-MM-DD")
            return 1
        raw["engine_freeze"] = frozen | {name: until}
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        print(f"engine_frozen: {name} until {until}" + (f" reason={reason}" if reason else ""))
        return 0
    if name not in frozen:
        print(f"engine unfreeze: {name} is not frozen")
        return 1
    frozen.pop(name)
    raw["engine_freeze"] = frozen
    raw.pop("engine_freeze", None) if not frozen else None
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    print(f"engine_unfrozen: {name}")
    return 0


def _safe_active_task(root, task_id):
    if not task_id:
        return None
    try:
        return require_task(root, task_id)
    except Exception:
        return None


def _print_status_issues(issues) -> int:
    if not status_has_problems(issues):
        return 0
    print()
    for issue in issues:
        print(issue.render())
    print(render_health_summary(issues))
    return 1


def status_full(workspace, root, config, state, runner, monitoring, issues):
    print(f"workspace: {workspace}")
    print("status_read_mode: full")
    print(f"default_engine: {config.default_engine}")
    freezes = active_engine_freezes(config)
    if freezes:
        for engine_name, until_dt in sorted(freezes.items()):
            local_until = until_dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")
            print(f"engine_frozen: {engine_name} until {local_until}")
    print(f"litehive_source_path: {config.litehive_source_path or '-'}")
    print(f"mode: {state.mode}")
    print(f"active_task_id: {state.active_task_id}")
    print(
        "runner_status: "
        f"{runner.status} pid={runner.pid or '-'} "
        f"started_at={runner.started_at or '-'} "
        f"heartbeat_at={runner.heartbeat_at or '-'} "
        f"active_task_id={runner.active_task_id or '-'}"
    )
    print(f"queued_tasks: {len(state.queue)}")
    print(f"pool_stop_reason: {state.pool_stop_reason}")
    for line in waiting_for_you_lines(root):
        print(line)
    if state.queue:
        print(f"queue_head: {state.queue[0]}")
    active_task = _safe_active_task(workspace, state.active_task_id)
    if active_task is not None:
        active_engine = (
            active_task.runtime.active_subagent.engine
            if active_task.runtime.active_subagent is not None
            else active_task.runtime.last_subagent.engine
            if active_task.runtime.last_subagent is not None
            else config.default_engine
        )
        active_stage = active_task.runtime.current_stage.step or active_task.pipeline_status or "-"
        print(f"active_task_title: {active_task.title}")
        print(f"active_task_status: {active_task.status}/{active_task.pipeline_status}")
        print(f"active_stage: {active_stage}")
        print(f"active_engine: {active_engine}")
    for line in render_engine_monitoring_lines(monitoring):
        print(line)
    print(f"default_retry_limit: {config.default_retry_limit}")
    print(f"retry_on: {format_retry_on(config)}")
    print(f"pool_stop_on_failure: {config.pool_stop_on_failure}")
    print(f"pool_max_tasks: {config.pool_max_tasks}")
    print(f"pool_stop_on_dirty_git: {config.pool_stop_on_dirty_git}")
    print(f"pool_stop_on_attention: {config.pool_stop_on_attention}")
    print(f"pool_selection_policy: {config.pool_selection_policy}")
    print(f"process_profile: {config.process_profile}")
    tasks = list_tasks(workspace)
    if tasks:
        print()
        for task in tasks:
            for line in render_task_summary(task, active=task.id == state.active_task_id, root=root):
                print(line)
    return _print_status_issues(issues)


def status_command(
    workspace: WorkspaceOption = Path.cwd(),
    full: Annotated[bool, typer.Option(help="Include the full per-task status dump.")] = False,
    fast: Annotated[bool, typer.Option(help="Deprecated compatibility alias")] = False,
) -> int:
    root = workspace.resolve()
    snapshot = collect_status_snapshot(root)
    config = snapshot.config
    state = snapshot.state
    runner = snapshot.runner
    monitoring = snapshot.monitoring
    if full:
        return status_full(workspace, root, config, state, runner, monitoring, snapshot.issues)

    active_task = _safe_active_task(workspace, state.active_task_id)
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
    print(f"repaired: {'yes' if summary.mutated else 'no'}")
    print(f"stale_runner_recovered: {'yes' if summary.stale_runner_recovered else 'no'}")
    print(f"cleared_active_task_id: {summary.cleared_active_task_id or '-'}")
    print("migrated_comment_tasks: " + (" ".join(summary.migrated_comment_task_ids) if summary.migrated_comment_task_ids else "-"))
    print("requeued_tasks: " + (" ".join(summary.requeued_task_ids) if summary.requeued_task_ids else "-"))
    print("removed_queue_entries: " + (" ".join(summary.removed_queue_entries) if summary.removed_queue_entries else "-"))
    print("deduped_queue_entries: " + (" ".join(summary.deduped_queue_entries) if summary.deduped_queue_entries else "-"))
    print("restored_queue_entries: " + (" ".join(summary.restored_queue_entries) if summary.restored_queue_entries else "-"))
    print("finalized_commit_tasks: " + (" ".join(summary.finalized_commit_task_ids) if summary.finalized_commit_task_ids else "-"))
    print("stale_process_tasks: " + (" ".join(summary.stale_process_task_ids) if summary.stale_process_task_ids else "-"))
    print("reassigned_duplicate_ids: " + (" ".join(summary.reassigned_duplicate_ids) if summary.reassigned_duplicate_ids else "-"))
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
    active_task = require_task(root, state.active_task_id) if state.active_task_id else None
    flagged_tasks = [task for task in tasks if task.status == "flagged"]
    worktrees = collect_managed_worktrees(root)
    dirty_report = inspect_dirty_worktree_gate(root)
    quota_health = _collect_quota_health()
    completed = sorted((task for task in tasks if task.status == "done"), key=lambda task: task.updated_at or "", reverse=True)[:3]

    print("=== Workspace Health ===")
    print(f"workspace: {root}")

    print()
    print("=== Active Task ===")
    if active_task is None:
        print("active_task: none")
    else:
        stage = _active_stage(active_task)
        print(f"active_task: {active_task.id} [{active_task.status}/{active_task.pipeline_status}] stage={stage} title={active_task.title}")

    print()
    print("=== Flagged Tasks ===")
    print(f"flagged_count: {len(flagged_tasks)}")
    if not flagged_tasks:
        print("flagged: none")
    else:
        for task in flagged_tasks:
            print(f"flagged: {task.id} stage={_active_stage(task)} reason={task.flag_reason or 'unknown'} last_verdict={_last_verdict(task)} summary={_last_summary(task)}")

    print()
    print("=== Worktrees ===")
    print(f"worktree_count: {len(worktrees)}")
    if not worktrees:
        print("worktree: none")
    else:
        for item in worktrees:
            print(f"worktree: {item.task_id} status={item.status} changes={item.change_count} active={'yes' if item.active else 'no'} path={item.worktree_rel}")

    print()
    print("=== Worktree Findings ===")
    if dirty_report.is_clean:
        print("worktree_findings: clean")
    else:
        for finding in dirty_report.findings:
            details = [f"location={finding.location_kind}", f"ownership={finding.ownership}"]
            if finding.task_id:
                details.append(f"task_id={finding.task_id}")
            if finding.worktree_path:
                details.append(f"path={finding.worktree_path}")
            details.append("dirty_paths=" + (",".join(finding.dirty_paths) if finding.dirty_paths else "-"))
            print("finding: " + " ".join(details))

    print()
    print("=== Engine Quotas ===")
    for quota in quota_health:
        print(f"quota: {quota.engine} status={quota.status} summary={quota.summary}")

    print()
    print("=== Daemon ===")
    daemon_lines = daemon_status_lines(root)
    daemon_status = _daemon_value(daemon_lines, "daemon_status") or "stopped"
    daemon_pid = _daemon_value(daemon_lines, "pid") or "-"
    print(f"daemon_status: {daemon_status}")
    print(f"daemon_pid: {daemon_pid}")

    print()
    print("=== Recent Completions ===")
    if not completed:
        print("completed: none")
    else:
        for task in completed:
            print(f"completed: {task.id} title={task.title} when={task.updated_at or '-'} summary={_last_summary(task)}")

    has_quota_problem = any(item.problem for item in quota_health)
    has_worktree_problem = any(
        finding.ownership in {"main-checkout", "ambiguous-ownership", "missing-recorded-worktree"}
        for finding in dirty_report.findings
    )
    return 1 if flagged_tasks or has_worktree_problem or has_quota_problem else 0


def _active_stage(task) -> str:
    return task.runtime.current_stage.step or task.pipeline_status or "-"


def _last_verdict(task) -> str:
    return task.runtime.last_stage.verdict or task.runtime.last_outcome.kind or "-"


def _last_summary(task) -> str:
    return task.runtime.last_stage.summary or task.runtime.last_outcome.reason or task.flag_reason or "-"


def _daemon_value(lines: list[str], key: str) -> str | None:
    prefix = f"{key}: "
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _collect_quota_health() -> list[_QuotaHealth]:
    zai_status = check_zai_quota()
    snapshots = {
        "claude": _claude_quota_health(),
        "codex": _codex_quota_health(),
        "copilot": _copilot_quota_health(),
        "gemini": _QuotaHealth(engine="gemini", status="unsupported", summary="no proactive quota check"),
        "goz": _zai_quota_health("goz", zai_status),
        "opencode": _zai_quota_health("opencode", zai_status),
    }
    return [snapshots[engine] for engine in ENGINE_CHOICES]


def _codex_quota_health() -> _QuotaHealth:
    status = check_codex_quota()
    if status.error is not None:
        return _QuotaHealth("codex", "unavailable", status.error)
    summary = f"short={status.short_term.percent_remaining:.1f}% remaining long={status.long_term.percent_remaining:.1f}% remaining reset={status.long_term.reset_at or '-'}"
    return _QuotaHealth("codex", "warning" if status.limit_reached else "ok", summary, status.limit_reached)


def _claude_quota_health() -> _QuotaHealth:
    status = check_claude_quota()
    if status.error is not None:
        return _QuotaHealth("claude", "unavailable", status.error)
    reset_at = status.long_term.reset_at or status.short_term.reset_at or "-"
    summary = f"short={status.short_term.percent_remaining:.1f}% remaining long={status.long_term.percent_remaining:.1f}% remaining reset={reset_at}"
    return _QuotaHealth("claude", "warning" if status.limit_reached else "ok", summary, status.limit_reached)


def _copilot_quota_health() -> _QuotaHealth:
    status = check_copilot_quota()
    if status.error is not None:
        return _QuotaHealth("copilot", "unavailable", status.error)
    summary = f"short={status.short_term.percent_remaining:.1f}% remaining long={status.long_term.percent_remaining:.1f}% remaining reset={status.long_term.reset_at or '-'}"
    return _QuotaHealth("copilot", "warning" if status.limit_reached else "ok", summary, status.limit_reached)


def _zai_quota_health(engine: str, status) -> _QuotaHealth:
    if status.error is not None:
        return _QuotaHealth(engine, "unavailable", status.error)
    summary = f"short={status.short_term.percent_remaining:.1f}% remaining long={status.long_term.percent_remaining:.1f}% remaining"
    return _QuotaHealth(engine, "warning" if status.limit_reached else "ok", summary, status.limit_reached)
