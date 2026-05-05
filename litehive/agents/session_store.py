"""SQLite-backed storage for structured subagent session artifacts."""

import json
from pathlib import Path
from typing import Any

from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow


_UNSET = object()
_EVENT_STREAM_KEY = "event_stream"


def _load_subagent_payload(root: Path, task_id: str, subagent_id: str) -> tuple[dict[str, Any], str | None]:
    with connect_workspace_db(root) as connection:
        row = connection.execute(
            """
            SELECT created_at, payload
            FROM subagent_sessions
            WHERE task_id = ? AND subagent_id = ?
            """,
            (task_id, subagent_id),
        ).fetchone()
    if row is None:
        return {}, None
    payload = json.loads(row["payload"])
    return payload if isinstance(payload, dict) else {}, row["created_at"]


def load_subagent_artifacts(root: Path, task_id: str, subagent_id: str) -> dict[str, Any]:
    payload, _ = _load_subagent_payload(root, task_id, subagent_id)
    return payload


def save_subagent_artifacts(
    root: Path,
    task_id: str,
    subagent_id: str,
    session: dict[str, Any] | object = _UNSET,
    report: dict[str, Any] | object = _UNSET,
    event_stream: dict[str, Any] | None | object = _UNSET,
) -> None:
    payload, created_at = _load_subagent_payload(root, task_id, subagent_id)
    if session is not _UNSET:
        payload["session"] = session
    if report is not _UNSET:
        payload["report"] = report
    if event_stream is not _UNSET:
        if event_stream is None:
            payload.pop(_EVENT_STREAM_KEY, None)
        else:
            payload[_EVENT_STREAM_KEY] = event_stream
    now = utcnow()
    created_at = created_at or now
    with connect_workspace_db(root) as connection:
        connection.execute(
            """
            INSERT INTO subagent_sessions (
                task_id,
                subagent_id,
                created_at,
                updated_at,
                payload
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id, subagent_id) DO UPDATE SET
                updated_at = excluded.updated_at,
                payload = excluded.payload
            """,
            (
                task_id,
                subagent_id,
                created_at,
                now,
                json.dumps(payload, default=str, sort_keys=True),
            ),
        )


def load_subagent_session(root: Path, task_id: str, subagent_id: str) -> dict[str, Any]:
    payload = load_subagent_artifacts(root, task_id, subagent_id)
    session = payload.get("session")
    return session if isinstance(session, dict) else {}


def load_subagent_report(root: Path, task_id: str, subagent_id: str) -> dict[str, Any]:
    payload = load_subagent_artifacts(root, task_id, subagent_id)
    report = payload.get("report")
    return report if isinstance(report, dict) else {}


def load_subagent_event_stream(root: Path, task_id: str, subagent_id: str) -> dict[str, Any]:
    payload = load_subagent_artifacts(root, task_id, subagent_id)
    event_stream = payload.get(_EVENT_STREAM_KEY)
    return event_stream if isinstance(event_stream, dict) else {}
