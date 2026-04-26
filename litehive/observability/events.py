"""SQLite event persistence for task lifecycle and subagent sessions."""

import json
import os
from pathlib import Path
from typing import Any

from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow
from litehive.domain.task import TaskRecord


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
    with connect_workspace_db(root) as connection:
        connection.execute(
            """
            INSERT INTO events (task_id, created_at, event_kind, payload)
            VALUES (?, ?, ?, ?)
            """,
            (task.id, event["ts"], kind, json.dumps(event, default=str, sort_keys=True)),
        )
        connection.commit()
    return event


def read_events(root: Path, task: TaskRecord) -> list[dict[str, Any]]:
    """Read all events for a task from SQLite."""
    with connect_workspace_db(root) as connection:
        rows = connection.execute(
            """
            SELECT payload
            FROM events
            WHERE task_id = ?
            ORDER BY id ASC
            """,
            (task.id,),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def last_event_timestamp(root: Path, task: TaskRecord) -> str | None:
    """Return the timestamp of the last persisted event for a task.

    Returns ``None`` if no events exist.
    """
    with connect_workspace_db(root) as connection:
        row = connection.execute(
            """
            SELECT created_at
            FROM events
            WHERE task_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (task.id,),
        ).fetchone()
    if row is None:
        return None
    return str(row["created_at"])


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
