"""Logs command for daemon, task, and subagent artifacts."""

from datetime import datetime
from pathlib import Path
import time

from litehive.agents.session_store import load_subagent_session
from litehive.cli.task_debug_support import render_task_evidence
from litehive.config.paths import workspace_path
from litehive.daemon.logs import latest_run_all_log_dir
from litehive.state.records import get_task_record, list_tasks
from litehive.tasks.journal import render_task_journal
from litehive.tasks.paths import read_text_artifact, resolve_artifact_path, task_dir

_DEFAULT_TAIL_LINES = 40
FOLLOW_POLL_SECONDS = 0.1


def show_latest_daemon_log(root: Path) -> int:
    latest_dir = latest_run_all_log_dir(root)
    log_path = _latest_daemon_log_path(latest_dir)
    if log_path is None:
        print("No daemon run logs found.")
        return 0
    print(f"daemon log: {log_path}")
    print()
    print(_tail_text(read_text_artifact(log_path)))
    return 0


def list_daemon_sessions(root: Path) -> int:
    logs_root = workspace_path(root.resolve(), "logs", "run-all")
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


def show_task_journal(root: Path, task) -> int:
    journal = render_task_journal(root, task)
    if not journal:
        print(f"{task.id}: journal not found")
        return 0
    print(journal)
    return 0


def show_latest_subagent(root: Path, task) -> int:
    return render_task_evidence(root, task)


def list_task_subagents(root: Path, task) -> int:
    if not task.subagents:
        print(f"{task.id}: no subagents")
        return 0

    runtime_by_id = {}
    if task.runtime.execution.active_subagent is not None:
        runtime_by_id[task.runtime.execution.active_subagent.id] = task.runtime.execution.active_subagent

    for ref in reversed(task.subagents):
        runtime_state = runtime_by_id.get(ref.id)
        session = load_subagent_session(root, task.id, ref.id)
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


def follow_active_subagent(root: Path, *, task_id: str | None = None) -> int:
    task = resolve_follow_task(root, task_id=task_id)
    ref = None if task is None else _latest_subagent_ref(task)
    is_active = bool(
        task is not None
        and ref is not None
        and task.runtime.execution.active_subagent is not None
        and task.runtime.execution.active_subagent.id == ref.id
    )
    if task is None or ref is None:
        print("No active subagent.")
        return 0

    active_task_id = task.id
    active_subagent_id = ref.id
    active_path = ref.path
    base = task_dir(root, task) / active_path
    stdout_path = _artifact_for_kind(base, "stdout", active=is_active)
    if stdout_path is None:
        print("Active subagent stdout not found.")
        return 0

    print(f"following: {stdout_path.relative_to(root)}")
    position = 0
    if not is_active:
        _print_follow_chunk(stdout_path, position)
        return 0

    while True:
        position = _print_follow_chunk(stdout_path, position)
        task = resolve_follow_task(root, task_id=task_id)
        if task is None or task.runtime.execution.active_subagent is None:
            position = _print_follow_chunk(stdout_path, position)
            break
        if task.id != active_task_id or task.runtime.execution.active_subagent.id != active_subagent_id:
            position = _print_follow_chunk(stdout_path, position)
            break
        if task.runtime.execution.active_subagent.path != active_path:
            position = _print_follow_chunk(stdout_path, position)
            break
        time.sleep(FOLLOW_POLL_SECONDS)
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


def _print_follow_chunk(stdout_path: Path, position: int) -> int:
    if not stdout_path.exists():
        return position
    content = stdout_path.read_text(encoding="utf-8")
    if len(content) <= position:
        return position
    print(content[position:], end="")
    return len(content)


def _session_outcome(directory: Path) -> str:
    post_status = sorted(directory.glob("*-post-status.log"))
    for path in reversed(post_status):
        for line in read_text_artifact(path).splitlines():
            if line.startswith("pool_stop_reason:"):
                value = line.split(":", 1)[1].strip()
                return value or "-"
    run_logs = sorted(directory.glob("*-run.log"))
    for path in reversed(run_logs):
        for line in reversed(read_text_artifact(path).splitlines()):
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
    if task.runtime.execution.active_subagent is not None:
        preferred_ids.append(task.runtime.execution.active_subagent.id)
    for subagent_id in preferred_ids:
        for ref in reversed(task.subagents):
            if ref.id == subagent_id:
                return ref
    return task.subagents[-1] if task.subagents else None


def _artifact_for_kind(base: Path, kind: str, *, active: bool) -> Path | None:
    if kind == "stdout":
        if active:
            live = resolve_artifact_path(base, "stdout.log")
            if live is not None:
                return live
        legacy = resolve_artifact_path(base, "stdout.txt")
        if legacy is not None:
            return legacy
        return resolve_artifact_path(base, "stdout.log")
    raise ValueError(f"Unsupported artifact kind: {kind}")


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


def resolve_follow_task(root: Path, *, task_id: str | None) -> object | None:
    if task_id is not None:
        return get_task_record(root, task_id)
    tasks = list_tasks(root, strict=False)
    active = next((task for task in tasks if task.runtime.execution.active_subagent is not None), None)
    if active is not None:
        return active
    return next((task for task in tasks if task.subagents), None)


def load_task_with_runtime(root: Path, task_id: str):
    return get_task_record(root, task_id)


def _coerce_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
