from dataclasses import dataclass
from typing import Protocol

from litehive.domain.common import PipelineState, utcnow
from litehive.workspace import Workspace


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

    def resumable(self) -> bool:
        """Return True when the session has at least one engine-issued continuation handle so the AgentNode can pass ``--continue``/``--resume`` on the next turn instead of starting a fresh conversation."""
        return self.engine_session_id is not None or self.conversation_id is not None


class SessionStore(Protocol):
    """Keyed by ``(task_id, node_name, engine_name)``.

    The AgentNode looks up a session for the engine it's about to call; same
    engine, same session across retries; different engine, different session.
    The task_id key prevents session handles from leaking across tasks in a
    shared persistent store.
    """

    def get_or_create(self, task_id: str, node_name: PipelineState, engine_name: str) -> Session:
        """Return the persisted session for ``(task_id, node_name, engine_name)`` or a fresh empty ``Session`` if none exists yet; AgentNode calls this before every engine turn so retries on the same engine reuse continuation handles and engine switches start clean."""
        ...

    def persist(self, task_id: str, node_name: PipelineState, engine_name: str, session: Session) -> None:
        """Write the session's continuation handles back to storage after the engine adapter has filled them in on the first turn; AgentNode calls this immediately after the turn so a crash before the next turn does not lose the resume token."""
        ...

    def clear_node_sessions(self, task_id: str, node_name: PipelineState) -> None:
        """Drop every engine's session for one ``(task_id, node_name)`` so the next entry to that stage starts conversations from scratch; called when the orchestrator decides the node's prior conversations are no longer relevant (e.g. recovery hijack, stage reset)."""
        ...


class SqliteSessionStore:
    """Persists ``Session`` rows to the ``pipeline_sessions`` sqlite table.

    Each row is one ``(task_id, node_name, engine_name)`` tuple.
    ``get_or_create`` returns a fresh ``Session`` when the row doesn't exist
    yet, without writing; the adapter writes on ``persist`` after the first
    turn fills in engine continuation state.
    """

    def __init__(self, workspace: Workspace) -> None:
        """Bind the store to a workspace so every method below uses one shared sqlite connection factory; one ``SqliteSessionStore`` per workspace is the expected shape because the schema lives in the workspace's ``data.db``."""
        self.workspace = workspace

    def get_or_create(self, task_id: str, node_name: PipelineState, engine_name: str) -> Session:
        """Look up the ``pipeline_sessions`` row for this triple and rehydrate a ``Session``, returning a brand-new empty one when no row exists; deliberately does not write so a session that is created and never used does not leave a row behind."""
        with self.workspace.connect() as connection:
            row = connection.execute(
                """
                SELECT engine_session_id, conversation_id
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
        )

    def persist(self, task_id: str, node_name: PipelineState, engine_name: str, session: Session) -> None:
        """Upsert the session's continuation handles plus an ``updated_at`` timestamp; the ON CONFLICT clause makes repeated writes for the same ``(task, node, engine)`` cheap so AgentNode can call this every turn without reading first."""
        with self.workspace.connect() as connection:
            connection.execute(
                """
                INSERT INTO pipeline_sessions (
                    task_id, node_name, engine_name,
                    engine_session_id, conversation_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, node_name, engine_name) DO UPDATE SET
                    engine_session_id = excluded.engine_session_id,
                    conversation_id = excluded.conversation_id,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    str(node_name),
                    engine_name,
                    session.engine_session_id,
                    session.conversation_id,
                    utcnow(),
                ),
            )
            connection.commit()

    def clear_node_sessions(self, task_id: str, node_name: PipelineState) -> None:
        """Delete every engine's session row for ``(task_id, node_name)`` in one statement so the next entry to that stage starts conversations from scratch; the per-engine breadth is intentional because a stage reset invalidates whichever engine had been mid-conversation."""
        with self.workspace.connect() as connection:
            connection.execute(
                "DELETE FROM pipeline_sessions WHERE task_id = ? AND node_name = ?",
                (task_id, str(node_name)),
            )
            connection.commit()
