from dataclasses import dataclass

from litehive.agents import ENGINE_CHOICES
from heru.quota.claude_quota import check_claude_quota
from heru.quota.codex_quota import check_codex_quota
from heru.quota.copilot_quota import check_copilot_quota
from heru.quota.zai_quota import check_zai_quota
from litehive.config import ensure_workspace
from litehive.daemon import daemon_status_lines
from litehive.workspace.worktree_inspection import inspect_dirty_worktree_gate
from litehive.tasks import list_tasks_state_first, load_state, require_task

from litehive.cli.worktree import collect_managed_worktrees


@dataclass(slots=True)
class _QuotaHealth:
    engine: str
    status: str
    summary: str
    problem: bool = False


def cmd_health(workspace):
    ensure_workspace(workspace)
    root = workspace.resolve()
    state = load_state(root)
    tasks = list_tasks_state_first(root, state=state, include_runtime=True)
    active_task = require_task(root, state.active_task_id) if state.active_task_id else None
    flagged_tasks = [task for task in tasks if task.status == "flagged"]
    worktrees = collect_managed_worktrees(root)
    dirty_report = inspect_dirty_worktree_gate(root)
    quota_health = _collect_quota_health()
    completed = sorted(
        (task for task in tasks if task.status == "done"),
        key=lambda task: task.updated_at or "",
        reverse=True,
    )[:3]

    print("=== Workspace Health ===")
    print(f"workspace: {root}")

    print()
    print("=== Active Task ===")
    if active_task is None:
        print("active_task: none")
    else:
        stage = _active_stage(active_task)
        print(
            f"active_task: {active_task.id} [{active_task.status}/{active_task.pipeline_status}] "
            f"stage={stage} title={active_task.title}"
        )

    print()
    print("=== Flagged Tasks ===")
    print(f"flagged_count: {len(flagged_tasks)}")
    if not flagged_tasks:
        print("flagged: none")
    else:
        for task in flagged_tasks:
            print(
                f"flagged: {task.id} stage={_active_stage(task)} "
                f"reason={task.flag_reason or 'unknown'} "
                f"last_verdict={_last_verdict(task)} "
                f"summary={_last_summary(task)}"
            )

    print()
    print("=== Worktrees ===")
    print(f"worktree_count: {len(worktrees)}")
    if not worktrees:
        print("worktree: none")
    else:
        for item in worktrees:
            print(
                f"worktree: {item.task_id} status={item.status} changes={item.change_count} "
                f"active={'yes' if item.active else 'no'} path={item.worktree_rel}"
            )

    print()
    print("=== Worktree Findings ===")
    if dirty_report.is_clean:
        print("worktree_findings: clean")
    else:
        for finding in dirty_report.findings:
            details = [
                f"location={finding.location_kind}",
                f"ownership={finding.ownership}",
            ]
            if finding.task_id:
                details.append(f"task_id={finding.task_id}")
            if finding.worktree_path:
                details.append(f"path={finding.worktree_path}")
            details.append(
                "dirty_paths="
                + (",".join(finding.dirty_paths) if finding.dirty_paths else "-")
            )
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
            print(
                f"completed: {task.id} title={task.title} "
                f"when={task.updated_at or '-'} summary={_last_summary(task)}"
            )

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
    return (
        task.runtime.last_stage.summary
        or task.runtime.last_outcome.reason
        or task.flag_reason
        or "-"
    )


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
    summary = (
        f"short={status.short_term.percent_remaining:.1f}% remaining "
        f"long={status.long_term.percent_remaining:.1f}% remaining "
        f"reset={status.long_term.reset_at or '-'}"
    )
    return _QuotaHealth("codex", "warning" if status.limit_reached else "ok", summary, status.limit_reached)


def _claude_quota_health() -> _QuotaHealth:
    status = check_claude_quota()
    if status.error is not None:
        return _QuotaHealth("claude", "unavailable", status.error)
    reset_at = status.long_term.reset_at or status.short_term.reset_at or "-"
    summary = (
        f"short={status.short_term.percent_remaining:.1f}% remaining "
        f"long={status.long_term.percent_remaining:.1f}% remaining "
        f"reset={reset_at}"
    )
    return _QuotaHealth("claude", "warning" if status.limit_reached else "ok", summary, status.limit_reached)


def _copilot_quota_health() -> _QuotaHealth:
    status = check_copilot_quota()
    if status.error is not None:
        return _QuotaHealth("copilot", "unavailable", status.error)
    summary = (
        f"short={status.short_term.percent_remaining:.1f}% remaining "
        f"long={status.long_term.percent_remaining:.1f}% remaining "
        f"reset={status.long_term.reset_at or '-'}"
    )
    return _QuotaHealth("copilot", "warning" if status.limit_reached else "ok", summary, status.limit_reached)


def _zai_quota_health(engine: str, status) -> _QuotaHealth:
    if status.error is not None:
        return _QuotaHealth(engine, "unavailable", status.error)
    summary = (
        f"short={status.short_term.percent_remaining:.1f}% remaining "
        f"long={status.long_term.percent_remaining:.1f}% remaining"
    )
    return _QuotaHealth(engine, "warning" if status.limit_reached else "ok", summary, status.limit_reached)
