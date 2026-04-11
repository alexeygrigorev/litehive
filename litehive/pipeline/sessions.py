from dataclasses import dataclass, field
from typing import Any, Protocol

from .types import NodeName


@dataclass
class Session:
    """One agent conversation with one engine.

    ``engine_session_id`` is filled in by the engine adapter after the first
    turn. Subsequent turns use it to resume (e.g. ``codex --continue <id>``,
    ``claude --resume <id>``, etc.). A session belongs to exactly one engine
    — if the agent needs to switch engines (the current one is blocked), the
    ``SessionStore`` hands back a brand-new empty ``Session`` for the new
    engine. Retries on the same engine reuse the same session so the
    adapter can emit its continue flag.
    """

    engine_session_id: str | None = None
    conversation_id: str | None = None
    turn_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def resumable(self) -> bool:
        return self.engine_session_id is not None or self.conversation_id is not None


class SessionStore(Protocol):
    """Keyed by ``(node_name, engine_name)`` — one session per engine per node.

    The AgentNode looks up a session for the engine it's about to call; same
    engine, same session across retries; different engine, different session.
    """

    def get_or_create(self, node_name: NodeName, engine_name: str) -> Session: ...
    def persist(self, node_name: NodeName, engine_name: str, session: Session) -> None: ...


class InMemorySessionStore:
    """Reference implementation; real store reads/writes under .litehive/tasks/.../sessions/."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[NodeName, str], Session] = {}

    def get_or_create(self, node_name: NodeName, engine_name: str) -> Session:
        return self._sessions.setdefault((node_name, engine_name), Session())

    def persist(self, node_name: NodeName, engine_name: str, session: Session) -> None:
        self._sessions[(node_name, engine_name)] = session
