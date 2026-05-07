"""SQLite-backed storage for structured subagent session artifacts."""

import json
from typing import Any, Protocol, runtime_checkable

from litehive.domain.common import utcnow
from litehive.workspace import Workspace


_UNSET = object()
_EVENT_STREAM_KEY = "event_stream"


@runtime_checkable
class SerializableSubagentSession(Protocol):
    """
    Concrete session row object accepted by the persistence boundary.
    """

    def as_dict(self) -> dict[str, object]: ...


def _load_subagent_payload(workspace: Workspace, task_id: str, subagent_id: str) -> tuple[dict[str, Any], str | None]:
    """
    Read the raw payload row plus its original ``created_at``.

    ``save_subagent_artifacts`` upserts this row repeatedly during a
    subagent's life; preserving the original created_at on every
    update means the row keeps a stable creation timestamp even
    though the payload churns on every progress callback.
    """
    with workspace.connect() as connection:
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
    if isinstance(payload, dict):
        payload_value = payload
    else:
        payload_value = {}
    return payload_value, row["created_at"]


def load_subagent_artifacts(workspace: Workspace, task_id: str, subagent_id: str) -> dict[str, Any]:
    """Return the full structured payload (session + report + event stream) for a subagent run — consumed by status snapshots and stage prompt builders that need every slice at once."""
    payload, _ = _load_subagent_payload(workspace, task_id, subagent_id)
    return payload


def save_subagent_artifacts(
    workspace: Workspace,
    task_id: str,
    subagent_id: str,
    session: dict[str, Any] | SerializableSubagentSession | object = _UNSET,
    report: dict[str, Any] | object = _UNSET,
    event_stream: dict[str, Any] | None | object = _UNSET,
) -> None:
    """
    Merge-write the per-subagent payload row.

    The ``_UNSET`` sentinel lets the SubagentManager's session
    helpers update one slice of the artifact bundle (metadata only,
    or report only, or event-stream only) without clobbering the
    others. ``event_stream=None`` is the explicit "remove the key"
    signal — distinct from "leave it alone".
    """
    payload, created_at = _load_subagent_payload(workspace, task_id, subagent_id)
    if session is not _UNSET:
        if isinstance(session, SerializableSubagentSession):
            payload["session"] = session.as_dict()
        else:
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
    with workspace.connect() as connection:
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


def load_subagent_session(workspace: Workspace, task_id: str, subagent_id: str) -> dict[str, Any]:
    """Return only the engine session metadata slice (resume IDs, transcript pointer) — used by stages that need to resume an existing engine session without paying for the report and event-stream slices."""
    payload = load_subagent_artifacts(workspace, task_id, subagent_id)
    session = payload.get("session")
    if isinstance(session, dict):
        return session
    return {}


def load_subagent_report(workspace: Workspace, task_id: str, subagent_id: str) -> dict[str, Any]:
    """Return only the structured report slice (verdict, summary, diagnostics) — read by downstream stages and operator-facing status."""
    payload = load_subagent_artifacts(workspace, task_id, subagent_id)
    report = payload.get("report")
    if isinstance(report, dict):
        return report
    return {}


def load_subagent_event_stream(workspace: Workspace, task_id: str, subagent_id: str) -> dict[str, Any]:
    """Return only the event-stream slice (engine tool calls, stdout chunks) — used by ``litehive worktree`` and post-mortem inspection where the timeline is what matters."""
    payload = load_subagent_artifacts(workspace, task_id, subagent_id)
    event_stream = payload.get(_EVENT_STREAM_KEY)
    if isinstance(event_stream, dict):
        return event_stream
    return {}
