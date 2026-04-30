import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from litehive.db.schema import connect_workspace_db
from litehive.domain.common import PipelineState, utcnow


@dataclass
class Session:
    """One agent conversation with one engine for one task.

    ``engine_session_id`` is filled in by the engine adapter after the first
    turn. Subsequent turns use it to resume (e.g. ``codex --continue <id>``,
    ``claude --resume <id>``). A session belongs to exactly one engine — if
    the agent needs to switch engines (the current one is blocked), the
    ``SessionStore`` hands back a brand-new empty ``Session`` for the new
    engine. Retries on the same engine reuse the same session so the adapter
    can emit its continue flag.
    """

    engine_session_id: str | None = None
    conversation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def resumable(self) -> bool:
        return self.engine_session_id is not None or self.conversation_id is not None


class SessionStore(Protocol):
    """Keyed by ``(task_id, node_name, engine_name)``.

    The AgentNode looks up a session for the engine it's about to call; same
    engine, same session across retries; different engine, different session.
    The task_id key prevents session handles from leaking across tasks in a
    shared persistent store.
    """

    def get_or_create(self, task_id: str, node_name: PipelineState, engine_name: str) -> Session: ...

    def persist(self, task_id: str, node_name: PipelineState, engine_name: str, session: Session) -> None: ...

    def clear_node_sessions(self, task_id: str, node_name: PipelineState) -> None: ...


class InMemorySessionStore:
    """Reference implementation for tests; keeps everything in a dict."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, PipelineState, str], Session] = {}

    def get_or_create(self, task_id: str, node_name: PipelineState, engine_name: str) -> Session:
        return self._sessions.setdefault((task_id, node_name, engine_name), Session())

    def persist(self, task_id: str, node_name: PipelineState, engine_name: str, session: Session) -> None:
        self._sessions[(task_id, node_name, engine_name)] = session

    def clear_node_sessions(self, task_id: str, node_name: PipelineState) -> None:
        for key in [k for k in self._sessions if k[:2] == (task_id, node_name)]:
            del self._sessions[key]


class SqliteSessionStore:
    """Persists ``Session`` rows to the ``pipeline_sessions`` sqlite table.

    Each row is one ``(task_id, node_name, engine_name)`` tuple. ``metadata``
    is stored as a JSON blob. ``get_or_create`` returns a fresh ``Session``
    when the row doesn't exist yet, without writing; the adapter writes on
    ``persist`` after the first turn fills in ``engine_session_id``.
    """

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    def get_or_create(self, task_id: str, node_name: PipelineState, engine_name: str) -> Session:
        with connect_workspace_db(self.workspace_root) as connection:
            row = connection.execute(
                """
                SELECT engine_session_id, conversation_id, metadata
                FROM pipeline_sessions
                WHERE task_id = ? AND node_name = ? AND engine_name = ?
                """,
                (task_id, str(node_name), engine_name),
            ).fetchone()
        if row is None:
            return Session()
        return Session(
            engine_session_id=row["engine_session_id"],
            conversation_id=row["conversation_id"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    def persist(self, task_id: str, node_name: PipelineState, engine_name: str, session: Session) -> None:
        metadata_json = json.dumps(session.metadata, sort_keys=True)
        with connect_workspace_db(self.workspace_root) as connection:
            connection.execute(
                """
                INSERT INTO pipeline_sessions (
                    task_id, node_name, engine_name,
                    engine_session_id, conversation_id, metadata, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, node_name, engine_name) DO UPDATE SET
                    engine_session_id = excluded.engine_session_id,
                    conversation_id = excluded.conversation_id,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    str(node_name),
                    engine_name,
                    session.engine_session_id,
                    session.conversation_id,
                    metadata_json,
                    utcnow(),
                ),
            )
            connection.commit()

    def clear_node_sessions(self, task_id: str, node_name: PipelineState) -> None:
        with connect_workspace_db(self.workspace_root) as connection:
            connection.execute(
                "DELETE FROM pipeline_sessions WHERE task_id = ? AND node_name = ?",
                (task_id, str(node_name)),
            )
            connection.commit()
