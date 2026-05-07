"""SQLite-backed storage for structured subagent session artifacts."""

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from litehive.domain.agent import SubagentId
from litehive.domain.common import utcnow
from litehive.workspace import Workspace


_UNSET = object()
_EVENT_STREAM_KEY = "event_stream"


class SubagentArtifactSlice(str, Enum):
    """
    Named top-level slices inside one subagent artifact payload.
    """

    SESSION = "session"
    REPORT = "report"
    EVENT_STREAM = _EVENT_STREAM_KEY


@dataclass(frozen=True, slots=True)
class LoadedSubagentSession:
    """
    Typed view of one loaded engine-session metadata slice.

    ``created_at`` is normalized at the storage boundary so session
    writers do not need to inspect raw dictionaries to preserve the
    original creation timestamp.
    """

    values: dict[str, Any]
    created_at: str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any], persisted_created_at: str | None) -> "LoadedSubagentSession":
        value = payload.get(SubagentArtifactSlice.SESSION.value)
        if isinstance(value, dict):
            values = value
        else:
            values = {}
        session_created_at = values.get("created_at")
        if isinstance(session_created_at, str):
            created_at = session_created_at
        else:
            created_at = persisted_created_at
        return cls(values=values, created_at=created_at)

    def __bool__(self) -> bool:
        return bool(self.values)

    @property
    def subagent_id(self) -> SubagentId | None:
        value = self._non_empty_string("id")
        if value is None:
            return None
        return SubagentId(value)

    @property
    def role(self) -> str | None:
        return self._non_empty_string("role")

    @property
    def exit_code(self) -> int | None:
        value = self.values.get("exit_code")
        if isinstance(value, int):
            return value
        return None

    def _non_empty_string(self, key: str) -> str | None:
        value = self.values.get(key)
        if not isinstance(value, str):
            return None
        value = value.strip()
        if value:
            return value
        return None


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


def _load_subagent_artifact_slice(
    workspace: Workspace,
    task_id: str,
    subagent_id: str,
    artifact_slice: SubagentArtifactSlice,
) -> dict[str, Any]:
    """
    Return one dictionary slice from a subagent artifact payload.

    Slice readers use this instead of loading the public full-artifact
    payload and manually indexing it with a raw string key.
    """
    payload, _ = _load_subagent_payload(workspace, task_id, subagent_id)
    value = payload.get(artifact_slice.value)
    if isinstance(value, dict):
        return value
    return {}


def load_subagent_session_record(workspace: Workspace, task_id: str, subagent_id: str) -> LoadedSubagentSession:
    """
    Return the typed session metadata slice and row metadata.
    """
    payload, created_at = _load_subagent_payload(workspace, task_id, subagent_id)
    return LoadedSubagentSession.from_payload(payload, created_at)


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
    return load_subagent_session_record(workspace, task_id, subagent_id).values


def load_subagent_report(workspace: Workspace, task_id: str, subagent_id: str) -> dict[str, Any]:
    """Return only the structured report slice (verdict, summary, diagnostics) — read by downstream stages and operator-facing status."""
    return _load_subagent_artifact_slice(workspace, task_id, subagent_id, SubagentArtifactSlice.REPORT)


def load_subagent_event_stream(workspace: Workspace, task_id: str, subagent_id: str) -> dict[str, Any]:
    """Return only the event-stream slice (engine tool calls, stdout chunks) — used by ``litehive worktree`` and post-mortem inspection where the timeline is what matters."""
    return _load_subagent_artifact_slice(workspace, task_id, subagent_id, SubagentArtifactSlice.EVENT_STREAM)
