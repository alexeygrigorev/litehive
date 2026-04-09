"""Append-only JSONL event persistence for task lifecycle and subagent sessions."""

import json
import os
from pathlib import Path
from typing import Any

from litehive.models import TaskRecord, utcnow


def _events_path(root: Path, task: TaskRecord) -> Path:
    from litehive.tasks.paths import task_dir
    return task_dir(root, task) / "events.jsonl"


def append_event(
    root: Path,
    task: TaskRecord,
    kind: str,
    *,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a single event to the task's JSONL event stream.

    Returns the full event dict that was written.
    """
    event: dict[str, Any] = {
        "ts": utcnow(),
        "task_id": task.id,
        "kind": kind,
    }
    if data:
        event["data"] = data
    path = _events_path(root, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, default=str) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return event


def read_events(root: Path, task: TaskRecord) -> list[dict[str, Any]]:
    """Read all events from the task's JSONL event stream."""
    path = _events_path(root, task)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def last_event_timestamp(root: Path, task: TaskRecord) -> str | None:
    """Return the timestamp of the last persisted event for a task.

    Reads only the last line of the JSONL file for efficiency.
    Returns ``None`` if no events exist.
    """
    path = _events_path(root, task)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        return None
    if not raw:
        return None
    last_line = raw.rsplit("\n", 1)[-1].strip()
    if not last_line:
        return None
    try:
        event = json.loads(last_line)
    except json.JSONDecodeError:
        return None
    ts = event.get("ts")
    return ts if isinstance(ts, str) else None


def append_session_log(
    base: Path,
    name: str,
    content: str,
) -> None:
    """Append content to an append-only session log file (e.g. stdout.log, stderr.log).

    Creates the file if it doesn't exist.  Appends only the new delta.
    """
    if not content:
        return
    path = base / f"{name}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


def ensure_session_log(base: Path, name: str) -> Path:
    """Ensure an empty session log file exists (for tail -f on startup)."""
    path = base / f"{name}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    return path
