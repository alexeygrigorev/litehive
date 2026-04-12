"""Lightweight CLI entrypoint with a fast status path."""


import os
import sys
from pathlib import Path

import yaml

from litehive.attention import waiting_for_you_lines
from litehive.config import resolve_workspace
from litehive.observability.status_diagnostics import (
    collect_status_snapshot,
    render_health_summary,
    status_has_problems,
)


def _fast_runner_status(workspace: Path) -> dict:
    """Return runner liveness inferred from the lock file, without importing locking.py."""
    lock_path = workspace / ".litehive" / ".runner.lock"
    result: dict = {
        "state": "never_started",
        "pid": None,
        "started_at": None,
        "heartbeat_at": None,
    }
    if not lock_path.exists():
        return result
    try:
        data = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    if not isinstance(data, dict) or not data:
        result["state"] = "stopped"
        return result
    result["pid"] = data.get("pid")
    result["started_at"] = data.get("started_at")
    result["heartbeat_at"] = data.get("heartbeat_at")
    pid = result["pid"]
    if pid is None:
        result["state"] = "stopped"
        return result
    try:
        os.kill(int(pid), 0)
        result["state"] = "running"
    except (ProcessLookupError, ValueError, TypeError):
        result["state"] = "dead"
    except PermissionError:
        result["state"] = "running"
    return result


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
    state = snapshot.state.model_dump(mode="python")
    monitoring = snapshot.monitoring.model_dump(mode="python")

    active_task_id = state.get("active_task_id")
    queue = state.get("queue", []) or []
    stop_reason = state.get("pool_stop_reason")
    mode = state.get("mode", "implementation")
    default_engine = snapshot.config.default_engine

    print(f"workspace: {workspace}")
    print(f"default_engine: {default_engine}")
    print(f"mode: {mode}")
    print(f"active_task_id: {active_task_id if active_task_id is not None else 'None'}")
    print(f"queued_tasks: {len(queue)}")
    print(f"pool_stop_reason: {stop_reason if stop_reason is not None else 'None'}")
    for line in waiting_for_you_lines(workspace):
        print(line)
    if queue:
        print(f"queue_head: {queue[0]}")

    runner = _fast_runner_status(workspace)
    print(f"runner_status: {runner['state']}")
    if runner["pid"] is not None:
        print(f"runner_pid: {runner['pid']}")
    if runner["started_at"]:
        print(f"runner_started_at: {runner['started_at']}")
    if runner["heartbeat_at"]:
        print(f"runner_heartbeat_at: {runner['heartbeat_at']}")

    if active_task_id is not None:
        from litehive.tasks.crud import get_task

        task = get_task(workspace, active_task_id)
        if task is not None:
            stage = task.runtime.current_stage.step or task.pipeline_status or "-"
            engine = (
                task.runtime.active_subagent.engine
                if task.runtime.active_subagent is not None
                else task.runtime.last_subagent.engine
                if task.runtime.last_subagent is not None
                else default_engine
            )
            print(f"active_task_title: {task.title}")
            print(f"active_task_status: {task.status}/{task.pipeline_status}")
            print(f"active_stage: {stage}")
            print(f"active_engine: {engine}")
    for engine_name in sorted((monitoring.get("engines") or {}).keys()):
        record = monitoring["engines"][engine_name] or {}
        parts = [
            f"engine_monitoring: {engine_name}",
            f"source={record.get('source', 'local')}",
            f"invocations={record.get('invocation_count', 0)}",
            f"success={record.get('success_count', 0)}",
            f"failure={record.get('failure_count', 0)}",
            f"limits={record.get('limit_event_count', 0)}",
        ]
        if record.get("provider"):
            parts.append(f"provider={record['provider']}")
        if record.get("last_limit_kind"):
            parts.append(f"last_limit_kind={record['last_limit_kind']}")
        if record.get("last_limit_reason"):
            parts.append(f"last_limit_reason={record['last_limit_reason']}")
        usage = record.get("usage") or {}
        if isinstance(usage, dict):
            usage_parts: list[str] = []
            if usage.get("used") is not None:
                usage_parts.append(f"used={usage['used']}")
            if usage.get("limit") is not None:
                usage_parts.append(f"limit={usage['limit']}")
            if usage.get("remaining") is not None:
                usage_parts.append(f"remaining={usage['remaining']}")
            if usage.get("unit"):
                usage_parts.append(f"unit={usage['unit']}")
            if usage.get("reset_at"):
                usage_parts.append(f"reset_at={usage['reset_at']}")
            if usage_parts:
                parts.append("usage=" + ",".join(usage_parts))
        if record.get("last_task_id"):
            parts.append(f"last_task={record['last_task_id']}")
        if record.get("observed_at"):
            parts.append(f"observed_at={record['observed_at']}")
        print(" ".join(parts))
    if status_has_problems(snapshot.issues):
        print()
        for issue in snapshot.issues:
            print(issue.render())
        print(render_health_summary(snapshot.issues))
        return 1
    return 0


def main() -> int:
    argv = sys.argv[1:]

    if os.environ.get("LITEHIVE_AGENT_ROLE"):
        cmd = argv[0] if argv else None
        if cmd is None or cmd in ("--help", "-h"):
            print("Usage: litehive agent [report|update|close]")
            print("\nRun 'litehive agent --help' for details.")
            return 0
        if cmd in {"report", "update", "close"}:
            argv = ["agent", *argv]
            sys.argv = [sys.argv[0], *argv]
            cmd = "agent"
        if cmd != "agent":
            print("You are not authorized to perform this command.")
            return 1

    if argv and argv[0] == "status" and "--full" not in argv:
        return _fast_status(argv[1:])

    from litehive.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
