"""SQLite-backed storage for structured subagent session artifacts."""

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from litehive.domain.agent import SubagentId
from litehive.domain.common import utcnow
from litehive.workspace import Workspace


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
    def updated_at(self) -> str | None:
        return self._non_empty_string("updated_at")

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


@runtime_checkable
class SerializableSubagentReport(Protocol):
    """
    Concrete report payload object accepted by the persistence boundary.
    """

    def as_dict(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class SubagentArtifactPayload:
    """
    Explicit wrapper for legacy session/report payload dictionaries.
    """

    values: Mapping[str, Any]

    def as_dict(self) -> dict[str, object]:
        return dict(self.values)


@dataclass(frozen=True, slots=True)
class SubagentEventStreamPayload:
    """
    Typed event-stream payload accepted by the persistence boundary.
    """

    values: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)


@dataclass(frozen=True, slots=True)
class SubagentArtifactStore:
    """
    Persistence handle for one subagent belonging to one workspace task.

    Carrying ``workspace``, ``task_id``, and ``subagent_id`` together
    keeps artifact writes scoped to the concrete subagent instead of
    passing those identifiers through every session/report call.
    """

    workspace: Workspace
    task_id: str
    subagent_id: str

    def load_all(self) -> dict[str, Any]:
        payload, _ = _load_subagent_payload(self.workspace, self.task_id, self.subagent_id)
        return payload

    def load_session_record(self) -> LoadedSubagentSession:
        payload, created_at = _load_subagent_payload(self.workspace, self.task_id, self.subagent_id)
        return LoadedSubagentSession.from_payload(payload, created_at)

    def load_session(self) -> dict[str, Any]:
        return self.load_session_record().values

    def load_report(self) -> dict[str, Any]:
        return self._load_slice(SubagentArtifactSlice.REPORT)

    def load_event_stream(self) -> dict[str, Any]:
        return self._load_slice(SubagentArtifactSlice.EVENT_STREAM)

    def save(
        self,
        *,
        session: SerializableSubagentSession | None = None,
        report: SerializableSubagentReport | None = None,
        event_stream: SubagentEventStreamPayload | None = None,
        clear_event_stream: bool = False,
    ) -> None:
        """
        Merge-write the per-subagent payload row.
        """
        payload, created_at = _load_subagent_payload(self.workspace, self.task_id, self.subagent_id)
        if session is not None:
            payload[SubagentArtifactSlice.SESSION.value] = session.as_dict()
        if report is not None:
            payload[SubagentArtifactSlice.REPORT.value] = report.as_dict()
        if clear_event_stream:
            payload.pop(_EVENT_STREAM_KEY, None)
        if event_stream is not None:
            payload[_EVENT_STREAM_KEY] = event_stream.as_dict()
        now = utcnow()
        created_at = created_at or now
        with self.workspace.connect() as connection:
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
                    self.task_id,
                    self.subagent_id,
                    created_at,
                    now,
                    json.dumps(payload, default=str, sort_keys=True),
                ),
            )

    def _load_slice(self, artifact_slice: SubagentArtifactSlice) -> dict[str, Any]:
        payload, _ = _load_subagent_payload(self.workspace, self.task_id, self.subagent_id)
        value = payload.get(artifact_slice.value)
        if isinstance(value, dict):
            return value
        return {}


def subagent_artifacts(workspace: Workspace, task_id: str, subagent_id: str) -> SubagentArtifactStore:
    """
    Bind workspace/task/subagent identity for artifact persistence.
    """
    return SubagentArtifactStore(workspace=workspace, task_id=task_id, subagent_id=subagent_id)


def _load_subagent_payload(workspace: Workspace, task_id: str, subagent_id: str) -> tuple[dict[str, Any], str | None]:
    """
    Read the raw payload row plus its original ``created_at``.

    ``SubagentArtifactStore.save`` upserts this row repeatedly during a
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

