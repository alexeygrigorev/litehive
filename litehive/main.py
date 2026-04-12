"""Lightweight CLI entrypoint with a fast status path."""


import os
import sys
from pathlib import Path

import yaml

from litehive.config import load_config, resolve_workspace
from litehive.pipeline_old.recovery import status_attention_findings


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
    state_path = workspace / ".litehive" / "state.yaml"
    monitoring_path = workspace / ".litehive" / "engine-monitoring.yaml"

    state = yaml.safe_load(state_path.read_text()) if state_path.exists() else {}
    monitoring = yaml.safe_load(monitoring_path.read_text()) if monitoring_path.exists() else {}

    active_task_id = state.get("active_task_id")
    queue = state.get("queue", []) or []
    stop_reason = state.get("pool_stop_reason")
    mode = state.get("mode", "implementation")
    default_engine = load_config(workspace).default_engine

    print(f"workspace: {workspace}")
    print(f"default_engine: {default_engine}")
    print(f"mode: {mode}")
    print(f"active_task_id: {active_task_id if active_task_id is not None else 'None'}")
    print(f"queued_tasks: {len(queue)}")
    print(f"pool_stop_reason: {stop_reason if stop_reason is not None else 'None'}")
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
        tasks_root = workspace / ".litehive" / "tasks"
        matches = sorted(tasks_root.glob(f"{active_task_id}-*/task.yaml"))
        if matches:
            task_path = matches[0]
            task_data = yaml.safe_load(task_path.read_text()) or {}
            runtime_path = task_path.with_name("runtime.yaml")
            runtime = yaml.safe_load(runtime_path.read_text()) if runtime_path.exists() else {}
            current_stage = (runtime.get("current_stage") or {}).get("step")
            last_subagent = runtime.get("last_subagent") or {}
            active_subagent = runtime.get("active_subagent") or {}
            stage = current_stage or task_data.get("pipeline_status") or "-"
            engine = (
                active_subagent.get("engine")
                or last_subagent.get("engine")
                or default_engine
            )
            print(f"active_task_title: {task_data.get('title', '-')}")
            print(
                "active_task_status: "
                f"{task_data.get('status', '-')}/{task_data.get('pipeline_status', '-')}"
            )
            print(f"active_stage: {stage}")
            print(f"active_engine: {engine}")
    alerts = status_attention_findings(workspace, pool_stop_reason=stop_reason)
    attention_log = workspace / ".litehive" / "runtime" / "attention.log"
    if attention_log.exists():
        try:
            entries = [
                line.strip()
                for line in attention_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except Exception:
            entries = []
        for entry in entries[-5:]:
            alerts.append(f"attention: {entry}")
    if alerts:
        print()
        print("!!! ATTENTION REQUIRED !!!")
        for alert in alerts:
            print(f"  ⚠ {alert}")
        print()

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
    return 0


def main() -> int:
    argv = sys.argv[1:]

    if os.environ.get("LITEHIVE_AGENT_ROLE"):
        cmd = argv[0] if argv else None
        if cmd is None or cmd in ("--help", "-h"):
            print("Usage: litehive agent [report|update|close]")
            print("\nRun 'litehive agent --help' for details.")
            return 0
        if cmd != "agent":
            print("You are not authorized to perform this command.")
            return 1

    if argv and argv[0] == "status" and "--full" not in argv:
        return _fast_status(argv[1:])

    from litehive.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
