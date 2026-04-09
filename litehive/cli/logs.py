"""Logs command for daemon, task, and subagent artifacts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time

import yaml

from litehive.config import ensure_workspace, workspace_dir
from litehive.daemon import latest_run_all_log_dir
from litehive.tasks.crud import list_tasks_state_first
from litehive.tasks.paths import _read_text_artifact, _resolve_artifact_path, task_dir

_DEFAULT_TAIL_LINES = 40
_FOLLOW_POLL_SECONDS = 0.1


def _cmd_logs(args) -> int:
    ensure_workspace(args.workspace)

    if getattr(args, "follow", False):
        return _follow_active_subagent(args.workspace, task_id=getattr(args, "task_id", None))
    if getattr(args, "daemon", False):
        return _list_daemon_sessions(args.workspace)
    if getattr(args, "task_id", None):
        task = _load_task_with_runtime(args.workspace, args.task_id)
        if task is None:
            print(f"task not found: {args.task_id}")
            return 1
        if getattr(args, "agent", False):
            if getattr(args, "all", False):
                return _list_task_subagents(args.workspace, task)
            return _show_latest_subagent(args.workspace, task)
        return _show_task_journal(args.workspace, task)
    return _show_latest_daemon_log(args.workspace)


def _show_latest_daemon_log(root: Path) -> int:
    latest_dir = latest_run_all_log_dir(root)
    log_path = _latest_daemon_log_path(latest_dir)
    if log_path is None:
        print("No daemon run logs found.")
        return 0
    print(f"daemon log: {log_path.relative_to(root)}")
    print()
    print(_tail_text(_read_text_artifact(log_path)))
    return 0


def _list_daemon_sessions(root: Path) -> int:
    logs_root = workspace_dir(root.resolve()) / "logs" / "run-all"
    if not logs_root.exists():
        print("No daemon run logs found.")
        return 0

    directories = sorted((path for path in logs_root.iterdir() if path.is_dir()), reverse=True)[:5]
    if not directories:
        print("No daemon run logs found.")
        return 0

    for directory in directories:
        print(
            f"{directory.name}  timestamp={_format_session_timestamp(directory.name)}  "
            f"outcome={_session_outcome(directory)}"
        )
    return 0


def _show_task_journal(root: Path, task) -> int:
    journal_path = task_dir(root, task) / "journal.md"
    if not journal_path.exists():
        print(f"{task.id}: journal not found")
        return 0
    print(journal_path.read_text(encoding="utf-8"))
    return 0


def _show_latest_subagent(root: Path, task) -> int:
    ref = _latest_subagent_ref(task)
    if ref is None:
        print(f"{task.id}: no subagents")
        return 0

    is_active = bool(task.runtime.active_subagent and task.runtime.active_subagent.id == ref.id)
    base = task_dir(root, task) / ref.path
    transcript_path = _artifact_for_kind(base, "transcript", active=is_active)
    stdout_path = _artifact_for_kind(base, "stdout", active=is_active)

    print(f"task: {task.id}")
    print(f"subagent: {ref.id}")
    print(f"role: {ref.role}")
    print(f"engine: {ref.engine}")
    print(f"status: {ref.status}")

    _print_artifact_tail(transcript_path, "transcript")
    _print_artifact_tail(stdout_path, "stdout")
    return 0


def _list_task_subagents(root: Path, task) -> int:
    if not task.subagents:
        print(f"{task.id}: no subagents")
        return 0

    runtime_by_id = {}
    if task.runtime.active_subagent is not None:
        runtime_by_id[task.runtime.active_subagent.id] = task.runtime.active_subagent
    if task.runtime.last_subagent is not None:
        runtime_by_id[task.runtime.last_subagent.id] = task.runtime.last_subagent

    for ref in reversed(task.subagents):
        runtime_state = runtime_by_id.get(ref.id)
        session = _load_session_yaml(task_dir(root, task) / ref.path)
        exit_code = _pick_value(runtime_state, session, "exit_code")
        started_at = _pick_value(runtime_state, session, "started_at", "created_at")
        completed_at = _pick_value(runtime_state, session, "completed_at", "updated_at")
        duration = _format_duration(started_at, completed_at)
        exit_str = str(exit_code) if exit_code is not None else "-"
        print(
            f"{ref.id}  role={ref.role}  engine={ref.engine}  status={ref.status}  "
            f"exit_code={exit_str}  duration={duration}"
        )
    return 0


def _follow_active_subagent(root: Path, *, task_id: str | None = None) -> int:
    task = _resolve_follow_task(root, task_id=task_id)
    if task is None or task.runtime.active_subagent is None:
        print("No active subagent.")
        return 0

    active_task_id = task.id
    active_subagent_id = task.runtime.active_subagent.id
    active_path = task.runtime.active_subagent.path
    base = task_dir(root, task) / active_path
    stdout_path = _artifact_for_kind(base, "stdout", active=True)
    if stdout_path is None:
        print("Active subagent stdout not found.")
        return 0

    print(f"following: {stdout_path.relative_to(root)}")
    position = 0

    while True:
        if stdout_path.exists():
            content = stdout_path.read_text(encoding="utf-8")
            if len(content) > position:
                chunk = content[position:]
                print(chunk, end="")
                position = len(content)
        task = _resolve_follow_task(root, task_id=task_id)
        if task is None or task.runtime.active_subagent is None:
            break
        if task.id != active_task_id or task.runtime.active_subagent.id != active_subagent_id:
            break
        if task.runtime.active_subagent.path != active_path:
            break
        time.sleep(_FOLLOW_POLL_SECONDS)
    return 0


def _latest_daemon_log_path(latest_dir: Path | None) -> Path | None:
    if latest_dir is None or not latest_dir.exists():
        return None
    preferred = sorted(latest_dir.glob("*-run.log"))
    if preferred:
        return preferred[-1]
    candidates = sorted(path for path in latest_dir.iterdir() if path.is_file())
    return candidates[-1] if candidates else None


def _tail_text(text: str, *, lines: int = _DEFAULT_TAIL_LINES) -> str:
    text = text.rstrip("\n")
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _session_outcome(directory: Path) -> str:
    post_status = sorted(directory.glob("*-post-status.log"))
    for path in reversed(post_status):
        for line in _read_text_artifact(path).splitlines():
            if line.startswith("pool_stop_reason:"):
                value = line.split(":", 1)[1].strip()
                return value or "-"
    run_logs = sorted(directory.glob("*-run.log"))
    for path in reversed(run_logs):
        for line in reversed(_read_text_artifact(path).splitlines()):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip() == "stop_reason":
                return value.strip() or "-"
    return "-"


def _format_session_timestamp(name: str) -> str:
    try:
        return datetime.strptime(name, "%Y%m%dT%H%M%SZ").isoformat() + "Z"
    except ValueError:
        return "-"


def _latest_subagent_ref(task):
    preferred_ids: list[str] = []
    if task.runtime.active_subagent is not None:
        preferred_ids.append(task.runtime.active_subagent.id)
    if task.runtime.last_subagent is not None:
        preferred_ids.append(task.runtime.last_subagent.id)
    for subagent_id in preferred_ids:
        for ref in reversed(task.subagents):
            if ref.id == subagent_id:
                return ref
    return task.subagents[-1] if task.subagents else None


def _artifact_for_kind(base: Path, kind: str, *, active: bool) -> Path | None:
    if kind == "transcript":
        return _resolve_artifact_path(base, "transcript.md")
    if kind == "stdout":
        if active:
            live = _resolve_artifact_path(base, "stdout.log")
            if live is not None:
                return live
        return _resolve_artifact_path(base, "stdout.txt")
    raise ValueError(f"Unsupported artifact kind: {kind}")


def _print_artifact_tail(path: Path | None, label: str) -> None:
    if path is None:
        print(f"{label}: (not found)")
        return
    content = _read_text_artifact(path)
    if not content:
        print(f"{label}: (empty)")
        return
    print(f"{label}:")
    print(_tail_text(content))


def _load_session_yaml(base: Path) -> dict[str, object]:
    session_path = _resolve_artifact_path(base, "session.yaml")
    if session_path is None:
        return {}
    try:
        data = yaml.safe_load(_read_text_artifact(session_path))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _pick_value(runtime_state, session: dict[str, object], *keys: str):
    if runtime_state is not None:
        for key in keys:
            value = getattr(runtime_state, key, None)
            if value is not None:
                return value
    for key in keys:
        value = session.get(key)
        if value is not None:
            return value
    return None


def _format_duration(started_at: str | datetime | None, completed_at: str | datetime | None) -> str:
    if not started_at or not completed_at:
        return "-"
    try:
        start = _coerce_datetime(started_at)
        end = _coerce_datetime(completed_at)
    except ValueError:
        return "-"
    total_seconds = int((end - start).total_seconds())
    return f"{total_seconds}s" if total_seconds >= 0 else "-"


def _resolve_follow_task(root: Path, *, task_id: str | None) -> object | None:
    tasks = list_tasks_state_first(root, include_runtime=True)
    if task_id is not None:
        return next((task for task in tasks if task.id == task_id), None)
    return next((task for task in tasks if task.runtime.active_subagent is not None), None)


def _load_task_with_runtime(root: Path, task_id: str):
    return next((task for task in list_tasks_state_first(root, include_runtime=True) if task.id == task_id), None)


def _coerce_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
