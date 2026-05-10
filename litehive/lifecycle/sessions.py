from dataclasses import dataclass
from typing import Protocol

from litehive.domain.common import PipelineState, utcnow
from litehive.workspace import Workspace


@dataclass(frozen=True, slots=True)
class FreshEngineSession:
    """
    Lifecycle state for an engine turn without a continuation handle.
    """

    resume_session_id: None = None
    """Always None; signals no prior conversation to resume."""


@dataclass(frozen=True, slots=True)
class ResumableEngineSession:
    """
    Lifecycle state for an engine turn with a continuation handle.
    """

    resume_session_id: str
    """The engine-issued continuation handle to pass on the next turn."""


class EngineSessionContinuation(Protocol):
    """
    Start-vs-continue state exposed by ``Session``.
    """

    @property
    def resume_session_id(self) -> str | None: ...


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
    """The engine's resume handle (e.g. codex session id)."""
    conversation_id: str | None = None
    """A secondary conversation identifier some engines expose."""

    def continuation_state(self) -> EngineSessionContinuation:
        """
        Return whether the next engine turn should start fresh or resume.
        """
        if self.engine_session_id is not None:
            return ResumableEngineSession(self.engine_session_id)
        if self.conversation_id is not None:
            return ResumableEngineSession(self.conversation_id)
        return FreshEngineSession()

    def resume_session_id(self) -> str | None:
        """
        Return the engine resume id for the next turn, when available.
        """
        return self.continuation_state().resume_session_id

    def capture_engine_session_id(self, resume_id: str) -> None:
        """
        Store the latest engine continuation handle on this session.
        """
        self.engine_session_id = resume_id

    def resumable(self) -> bool:
        """
        Report whether the session has a continuation handle.

        AgentNode reads this before each turn: when at least one
        engine-issued handle exists, the next turn passes
        ``--continue``/``--resume`` so the engine resumes the prior
        conversation instead of starting a fresh one.
        """
        return isinstance(self.continuation_state(), ResumableEngineSession)


class SessionStore(Protocol):
    """Keyed by ``(task_id, node_name, engine_name)``.

    The AgentNode looks up a session for the engine it's about to call; same
    engine, same session across retries; different engine, different session.
    The task_id key prevents session handles from leaking across tasks in a
    shared persistent store.
    """

    def get_or_create(self, task_id: str, node_name: PipelineState, engine_name: str) -> Session:
        """
        Return the session for one task/node/engine triple.

        AgentNode calls this before every engine turn. A persisted row
        is rehydrated so retries on the same engine reuse continuation
        handles; a missing row produces a fresh empty ``Session`` so
        engine switches start clean instead of inheriting a dead
        resume id from a different engine.
        """
        ...

    def persist(self, task_id: str, node_name: PipelineState, engine_name: str, session: Session) -> None:
        """
        Save the session's continuation handles back to storage.

        AgentNode calls this immediately after each turn so a crash
        before the next turn does not lose the resume token; without
        the immediate persist, the next launch would ask the engine
        for a brand-new conversation and forget what the agent had
        already said.
        """
        ...

    def clear_node_sessions(self, task_id: str, node_name: PipelineState) -> None:
        """
        Drop every engine's session for one ``(task_id, node_name)``.

        Called when the orchestrator decides the node's prior
        conversations are no longer relevant (e.g. a cross-agent reject
        hands control to a different stage, or a recovery hijack
        invalidates the prior conversations) so the next entry to that
        stage starts every engine from scratch.
        """
        ...


class SqliteSessionStore:
    """Persists ``Session`` rows to the ``pipeline_sessions`` sqlite table.

    Each row is one ``(task_id, node_name, engine_name)`` tuple.
    ``get_or_create`` returns a fresh ``Session`` when the row doesn't exist
    yet, without writing; the adapter writes on ``persist`` after the first
    turn fills in engine continuation state.
    """

    def __init__(self, workspace: Workspace) -> None:
        """
        Bind the store to a workspace.

        One ``SqliteSessionStore`` per workspace is the expected shape
        because the ``pipeline_sessions`` schema lives in the
        workspace's ``data.db``; sharing one across workspaces would
        route session rows into the wrong db.
        """
        self.workspace = workspace

    def get_or_create(self, task_id: str, node_name: PipelineState, engine_name: str) -> Session:
        """
        Rehydrate the session row for this triple, or return a fresh
        empty ``Session`` when no row exists.

        Deliberately does not write on the missing-row path so a
        session that is created and never used does not leave a phantom
        row behind; the row only lands once ``persist`` is called with
        real continuation handles.
        """
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
        """
        Upsert the session's continuation handles plus an
        ``updated_at`` timestamp.

        The ON CONFLICT clause makes repeated writes for the same
        ``(task, node, engine)`` cheap so AgentNode can call this every
        turn without reading first; that write-every-turn pattern is
        what keeps the resume token current when a crash interrupts
        the next turn.
        """
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
        """
        Delete every engine's session row for ``(task_id, node_name)``.

        The per-engine breadth is intentional: a stage reset invalidates
        whichever engine had been mid-conversation, and we don't know
        which engine the next entry will pick. One DELETE statement
        across engines is also cheaper than one per engine.
        """
        with self.workspace.connect() as connection:
            connection.execute(
                "DELETE FROM pipeline_sessions WHERE task_id = ? AND node_name = ?",
                (task_id, str(node_name)),
            )
            connection.commit()
