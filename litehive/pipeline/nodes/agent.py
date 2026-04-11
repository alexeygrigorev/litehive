from dataclasses import dataclass, field
from typing import Any, Protocol

from ..events import Blocked, Crash, Event, Pass, Reject
from ..persistence import TaskState
from ..types import NodeName, NodeType
from .base import Node


# ── Error taxonomy ───────────────────────────────────────────────────────
#
# Engine adapters raise one of these to tell the AgentNode what to do next.
# No "tier 1 / 2 / 3" jargon — each class says what response it wants.


class TransientError(Exception):
    """Retry on the same engine, same session.

    Raised for transient faults: a flaky network, a malformed tool response,
    a retryable HTTP 5xx, an interrupted stream. The engine itself is fine —
    the agent just needs another shot with the same state.
    """


class EngineBlockedError(Exception):
    """Base class for 'this engine is unavailable — switch to another one'.

    Raised when the *engine* is the problem, not the work. The AgentNode
    excludes this engine from the remaining selector calls for this node
    visit and asks the selector for another one.
    """


class QuotaExceeded(EngineBlockedError):
    """Engine hit a quota / rate limit that won't clear soon enough."""


class EngineOverloaded(EngineBlockedError):
    """Engine responded with 'overloaded, try later'."""


class ModelUnavailable(EngineBlockedError):
    """The requested model isn't served by this engine right now."""


class UnrecoverableError(Exception):
    """Escalate to the state machine as a ``Crash`` event.

    Raised for errors that neither a same-engine retry nor a different
    engine will fix: a bug in the prompt, a broken task config, an assertion
    failure inside the adapter. The state machine routes this through the
    normal ``Crash → recovering`` path.
    """


@dataclass
class AgentVerdict:
    outcome: str  # "pass" | "reject" | "blocked"
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class Engine(Protocol):
    name: str

    def run_turn(self, session: Any, prompt: Any, state: TaskState) -> AgentVerdict: ...


class EngineSelector(Protocol):
    """Policy that picks an engine for a node.

    The selector decides based on task state, the node it's running for, and
    the set of engines already ruled out during this node visit (via
    ``EngineBlockedError``). Returns ``None`` when no engine is eligible —
    the agent then escalates as ``AllEnginesExhausted``.
    """

    def select(
        self,
        state: TaskState,
        node_name: NodeName,
        excluded: frozenset[str],
    ) -> Engine | None: ...


class SessionProvider(Protocol):
    def get_or_create(self, node_name: NodeName, engine_name: str) -> Any: ...
    def persist(self, node_name: NodeName, engine_name: str, session: Any) -> None: ...


class AgentNode(Node):
    """Base for agent-backed stages.

    Owns the two in-node error responses — **retry same engine** and
    **switch engine** — so the state machine only ever sees the resolved
    outcome (``Pass`` / ``Reject`` / ``Blocked`` / ``Crash``). The node
    never iterates an engine list: it asks the ``EngineSelector`` each time
    it needs one.

    Execution flow for one ``run()`` call:

    ::

        while:
          engine = selector.select(state, node, excluded)
          if engine is None:
              return Crash(AllEnginesExhausted)

          session = session_store.get_or_create(node, engine.name)
          for attempt in range(retry_budget):
              try: verdict = engine.run_turn(session, prompt, state)
                   return Pass / Reject / Blocked
              except TransientError:     continue        # retry same engine
                                                          # (adapter reuses
                                                          #  session id → --continue)
              except EngineBlockedError: break to outer  # exclude engine
              except UnrecoverableError: return Crash    # done
          else:
              # retry budget exhausted → treat as engine blocked
              break to outer, ask selector for next engine

    ``excluded`` carries across the outer loop so the selector won't return
    the same flaky engine twice. Only when every eligible engine has been
    excluded does the agent escalate with ``Crash(AllEnginesExhausted)``.
    """

    node_type = NodeType.AGENT

    def __init__(
        self,
        name: NodeName,
        selector: EngineSelector,
        session_provider: SessionProvider,
        *,
        retry_budget: int = 3,
        grace_period_seconds: int | None = None,
    ) -> None:
        self.name = name
        self.selector = selector
        self.sessions = session_provider
        self.retry_budget = retry_budget
        if grace_period_seconds is not None:
            self.grace_period_seconds = grace_period_seconds

    def build_prompt(self, state: TaskState) -> Any:
        raise NotImplementedError

    def run(self, state: TaskState) -> Event:
        prompt = self.build_prompt(state)
        excluded: set[str] = set()
        last_exc: Exception | None = None

        while True:
            engine = self.selector.select(state, self.name, frozenset(excluded))
            if engine is None:
                return Crash(
                    exc_type="AllEnginesExhausted",
                    message=str(last_exc) if last_exc else "no engine eligible",
                )

            # One session per (node, engine). Retries on the same engine reuse
            # it so the adapter can emit --continue; switching engines gets a
            # fresh one so we don't carry a dead session id across providers.
            session = self.sessions.get_or_create(self.name, engine.name)
            outcome = self._run_with_retries(engine, session, prompt, state)
            if isinstance(outcome, Event):
                return outcome

            # Engine blocked (or retry budget exhausted on this engine) →
            # exclude it and ask the selector for another.
            last_exc = outcome
            excluded.add(engine.name)

    def _run_with_retries(
        self,
        engine: Engine,
        session: Any,
        prompt: Any,
        state: TaskState,
    ) -> Event | EngineBlockedError:
        """Run one engine up to ``retry_budget`` times on its own session.

        Returns an ``Event`` when the outcome is resolved (verdict or
        ``UnrecoverableError`` → Crash), or an ``EngineBlockedError`` when the
        caller should ask the selector for a replacement engine. Retry-budget
        exhaustion on ``TransientError`` is folded into the engine-switch path
        — a persistently flaky engine is, in effect, blocked.
        """
        last_exc: Exception | None = None
        for _ in range(self.retry_budget):
            try:
                verdict = engine.run_turn(session, prompt, state)
                self.sessions.persist(self.name, engine.name, session)
                return self._verdict_to_event(verdict)
            except TransientError as exc:
                last_exc = exc
                continue  # retry same engine, same session (adapter uses --continue)
            except EngineBlockedError as exc:
                return exc  # caller asks selector for next engine
            except UnrecoverableError as exc:
                return Crash(exc_type=type(exc).__name__, message=str(exc))
        return EngineBlockedError(
            f"retry budget ({self.retry_budget}) exhausted on {engine.name}: {last_exc}"
        )

    def _verdict_to_event(self, verdict: AgentVerdict) -> Event:
        outcome = verdict.outcome.lower()
        if outcome == "pass":
            return Pass()
        if outcome == "reject":
            return Reject(source="agent", reason=verdict.reason)
        if outcome == "blocked":
            return Blocked(reason=verdict.reason)
        return Crash(exc_type="UnknownVerdict", message=verdict.outcome)
