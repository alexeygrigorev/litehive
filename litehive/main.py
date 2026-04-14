"""Lightweight CLI entrypoint with a fast status path."""


import os
import sys
from pathlib import Path

from litehive.attention import waiting_for_you_lines
from litehive.config.paths import workspace_runner_lock_path
from litehive.config.workspace import resolve_workspace
from litehive.domain.runtime import RunnerStatusState
from litehive.observability.engine_monitoring import render_engine_monitoring_lines
from litehive.observability.status import render_active_task_detail_lines
from litehive.observability.status_diagnostics import (
    collect_status_snapshot,
    render_health_summary,
    status_has_problems,
)


def _agent_command_is_allowed(role: str, argv: list[str]) -> bool:
    """Return whether an agent role may invoke a non-`agent` command."""
    if not argv:
        return False
    if role != "recovery":
        return False
    return tuple(argv[:2]) in {
        ("pipeline", "journal"),
        ("pipeline", "rules"),
        ("task", "logs"),
    }


def _fast_runner_state_label(workspace: Path, runner: RunnerStatusState) -> str:
    if runner.status in {"running", "late"}:
        return "running"
    if not workspace_runner_lock_path(workspace).exists():
        return "never_started"
    if runner.pid is None:
        return "stopped"
    return "dead"


def _workspace_override_from_argv(argv: list[str]) -> Path | None:
    for index, arg in enumerate(argv):
        if arg == "--workspace" and index + 1 < len(argv):
            return Path(argv[index + 1])
        elif arg.startswith("--workspace="):
            return Path(arg.split("=", 1)[1])
    return None


def _fast_status(argv: list[str]) -> int:
    try:
        workspace = resolve_workspace(None, workspace=_workspace_override_from_argv(argv))
    except ValueError as exc:
        print(f"status failed: {exc}")
        return 1
    snapshot = collect_status_snapshot(workspace)
    state = snapshot.state
    runner = snapshot.runner
    monitoring = snapshot.monitoring

    active_task_id = state.active_task_id
    queue = state.queue
    stop_reason = state.pool_stop_reason
    default_engine = snapshot.config.default_engine

    print(f"workspace: {workspace}")
    print(f"default_engine: {default_engine}")
    print("mode: implementation")
    print(f"active_task_id: {active_task_id if active_task_id is not None else 'None'}")
    print(f"queued_tasks: {len(queue)}")
    print(f"pool_stop_reason: {stop_reason if stop_reason is not None else 'None'}")
    for line in waiting_for_you_lines(workspace):
        print(line)
    if queue:
        print(f"queue_head: {queue[0]}")

    runner_state = _fast_runner_state_label(workspace, runner)
    print(f"runner_status: {runner_state}")
    if runner.pid is not None:
        print(f"runner_pid: {runner.pid}")
    if runner.started_at:
        print(f"runner_started_at: {runner.started_at}")
    if runner.heartbeat_at:
        print(f"runner_heartbeat_at: {runner.heartbeat_at}")

    if active_task_id is not None:
        from litehive.state.records import get_task

        task = get_task(workspace, active_task_id)
        for line in render_active_task_detail_lines(task, default_engine):
            print(line)
    for line in render_engine_monitoring_lines(monitoring):
        print(line)
    if status_has_problems(snapshot.issues):
        print()
        for issue in snapshot.issues:
            print(issue.render())
        print(render_health_summary(snapshot.issues))
        return 1
    return 0


def main() -> int:
    argv = sys.argv[1:]

    agent_role = os.environ.get("LITEHIVE_AGENT_ROLE")
    if agent_role:
        cmd = argv[0] if argv else None
        if cmd is None or cmd in ("--help", "-h"):
            print("Usage: litehive agent [report|update|close]")
            print("\nRun 'litehive agent --help' for details.")
            return 0
        if cmd in {"report", "update", "close"}:
            argv = ["agent", *argv]
            sys.argv = [sys.argv[0], *argv]
            cmd = "agent"
        if cmd != "agent" and not _agent_command_is_allowed(agent_role, argv):
            print("You are not authorized to perform this command.")
            return 1

    if argv and argv[0] == "status" and "--full" not in argv:
        return _fast_status(argv[1:])

    from litehive.cli.app import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
