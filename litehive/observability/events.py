"""
SQLite event persistence for task lifecycle and subagent sessions.

The append-only ``events`` table is the durable narrative of what
happened to a task — every transition, every subagent run.
Recovery and operator surfaces replay it when the task store is
out of date or wiped, which is why the writes go through a single
helper rather than scattered SQL.
"""

import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol

from litehive.domain.common import utcnow
from litehive.domain.task import TaskRecord
from litehive.workspace import Workspace


class PersistedTaskEvent(Protocol):
    """
    Typed event object accepted by ``append_event``.

    Concrete event classes own their payload fields and expose the
    serialized kind/data pair at the persistence boundary.
    """

    @property
    def kind(self) -> str:
        """
        String tag that identifies the event type (e.g. ``stage_completed``).

        Stored in the ``event_kind`` column so downstream readers can
        filter without parsing the full payload.
        """
        ...

    def data(self) -> Mapping[str, object]:
        """
        Return the structured payload fields the event carries.

        Empty when the event has no extra data beyond the standard
        ``ts`` / ``task_id`` / ``kind`` envelope.
        """
        ...


def append_event(
    workspace: Workspace,
    task: TaskRecord,
    event: PersistedTaskEvent,
) -> dict[str, Any]:
    """
    Append a single event to a task's durable event stream.

    Records the canonical ``(ts, task_id, kind[, data])`` shape
    so downstream readers can rebuild a coherent timeline. The
    full event dict is returned so callers can include it in a
    test assertion or pipe it into another sink without having
    to re-read the row.
    """
    payload: dict[str, Any] = {
        "ts": utcnow(),
        "task_id": task.id,
        "kind": event.kind,
    }
    data = event.data()
    if data:
        payload["data"] = dict(data)
    with workspace.connect() as connection:
        connection.execute(
            """
            INSERT INTO events (task_id, created_at, event_kind, payload)
            VALUES (?, ?, ?, ?)
            """,
            (task.id, payload["ts"], payload["kind"], json.dumps(payload, default=str, sort_keys=True)),
        )
        connection.commit()
    return payload


def read_events(workspace: Workspace, task: TaskRecord) -> list[dict[str, Any]]:
    """
    Read all events for a task in insertion order.

    Skips rows whose payload fails to round-trip through JSON
    (legacy or corrupted data) so a single bad row cannot block
    the whole replay path. Replay-driven recovery and operator
    inspection both call this; keeping the reader tolerant lets
    them make progress on otherwise-readable history.
    """
    with workspace.connect() as connection:
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


def last_event_timestamp(workspace: Workspace, task: TaskRecord) -> str | None:
    """
    Return the timestamp of the last persisted event for a task.

    Returns ``None`` when no events exist so the caller can
    distinguish "never seen" from "stale". Used to drive
    inactivity timeouts; reading the most recent ``created_at``
    via SQL is cheaper than loading every event just to inspect
    the tail.
    """
    with workspace.connect() as connection:
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
    """
    Append content to a subagent session log file.

    Used for ``stdout.log``/``stderr.log``-style streams. Goes
    through ``os.open`` with ``O_APPEND`` rather than open-mode
    ``"a"`` so concurrent writers (the live subagent and a
    follower writing trace markers) cannot tear each other's
    writes. Skips the write when ``content`` is empty so empty
    polls are no-ops.
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
    """
    Ensure an empty session log file exists.

    Lets ``tail -f`` (or :func:`follow_active_subagent`) attach
    on subagent startup before the first byte is written;
    without the placeholder, the follower would spin in a
    "file not found" loop until the subagent produced output.
    """
    path = base / f"{name}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    return path
