import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal, Protocol

from litehive.domain.common import PipelineState
from ..events import Blocked, Crash, Event, Pass, Reject
from ..persistence import TaskState
from ..types import NodeType
from .base import Node


# ── Error taxonomy ───────────────────────────────────────────────────────
#
# Engine adapters raise one of these to tell the AgentNode what to do next.
# Each class name is the response the node takes.


class TransientError(Exception):
    """Retry on the same engine, same session.

    Raised for transient faults: a flaky network, a malformed tool response,
    a retryable HTTP 5xx, an interrupted stream. The engine itself is fine —
    the agent just needs another shot with the same state.
    """

    def __init__(self, message: str, failure_kind: str | None = None) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


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


class NudgeRequired(Exception):
    """Agent finished its turn without submitting a verdict — nudge it.

    The adapter raises this when the agent exited cleanly but never called
    ``litehive agent report``. The AgentNode reissues the turn on the same
    session (so the engine can resume via --continue) with a prompt that
    reminds the agent to submit a verdict. A separate ``nudge_budget``
    applies; nudges do not consume the retry budget. If nudges run out the
    stage crashes with ``NudgeBudgetExhausted``.
    """


@dataclass
class AgentVerdict:
    outcome: str  # "pass" | "reject"
    reason: str = ""
    classification: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source: Literal["agent", "guard", "hook", "system"] = "agent"


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
        node_name: PipelineState,
        excluded: frozenset[str],
    ) -> Engine | None: ...


class SessionProvider(Protocol):
    def get_or_create(self, task_id: str, node_name: PipelineState, engine_name: str) -> Any: ...

    def persist(self, task_id: str, node_name: PipelineState, engine_name: str, session: Any) -> None: ...


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
        name: PipelineState,
        selector: EngineSelector,
        session_provider: SessionProvider,
        retry_budget: int = 3,
        retry_on: tuple[str, ...] = ("execution_limit", "timeout"),
        retry_backoff_seconds: float = 0.0,
        retry_backoff_multiplier: float = 2.0,
        nudge_budget: int = 1,
        sleep_fn: Callable[[float], None] | None = None,
        grace_period_seconds: int | None = None,
    ) -> None:
        """Wire the node to its engine selector, session store, and retry budgets; the lifecycle factory constructs one ``AgentNode`` per role-stage and reuses it across tasks."""
        self.name = name
        self.selector = selector
        self.sessions = session_provider
        self.retry_budget = retry_budget
        self.retry_on = frozenset(retry_on)
        self.retry_backoff_seconds = retry_backoff_seconds
        self.retry_backoff_multiplier = retry_backoff_multiplier
        # Default is 1 and that's almost always the right choice: if the agent
        # ignores the first reminder to submit a verdict, a second reminder is
        # unlikely to land any better. Exposed as a parameter so tests and
        # unusual deployments can tune it.
        self.nudge_budget = nudge_budget
        self._sleep = sleep_fn or time.sleep
        if grace_period_seconds is not None:
            self.grace_period_seconds = grace_period_seconds

    def build_prompt(self, state: TaskState) -> Any:
        """Hook subclasses override to assemble the role-specific prompt; the base raises so a misregistered node fails loudly instead of running an empty turn."""
        raise NotImplementedError

    def build_nudge_prompt(self, state: TaskState, original_prompt: Any) -> Any:
        """Return a variant of the original prompt that reminds the agent to report.

        The default implementation works for typed ``AgentPrompt`` payloads
        produced by ``RoleAgent.build_prompt`` and the legacy dict-shaped
        prompts emitted by test stubs: in both cases it sets a ``nudge``
        flag the serializer can surface. Subclasses with non-dict prompts
        must override.
        """
        del state
        from .. import prompt_types  # noqa: PLC0415  (avoid cycle)

        if isinstance(original_prompt, prompt_types.AgentPrompt):
            return replace(
                original_prompt,
                nudge=True,
                nudge_message=(
                    "You finished your last turn without submitting a verdict via "
                    "`litehive agent report --verdict <pass|reject>`. Please "
                    "review your work and submit your verdict now."
                ),
            )
        if isinstance(original_prompt, dict):
            nudged = dict(original_prompt)
            nudged["nudge"] = True
            nudged["nudge_message"] = (
                "You finished your last turn without submitting a verdict via "
                "`litehive agent report --verdict <pass|reject>`. Please "
                "review your work and submit your verdict now."
            )
            return nudged
        return original_prompt

    def run(self, state: TaskState) -> Event:
        """Drive the engine-selection outer loop: build the prompt once, then keep asking the selector for a fresh engine until one resolves to an Event or every engine is excluded."""
        prompt = self.build_prompt(state)
        excluded: set[str] = set()
        last_exc: Exception | None = None

        while True:
            engine = self.selector.select(state, self.name, frozenset(excluded))
            if engine is None:
                if last_exc:
                    crash_message = str(last_exc)
                else:
                    crash_message = "no engine eligible"
                return Crash(
                    exc_type="AllEnginesExhausted",
                    message=crash_message,
                )

            # One session per (task, node, engine). Retries on the same engine
            # reuse it so the adapter can emit --continue; switching engines
            # gets a fresh one so we don't carry a dead session id across
            # providers. The task_id key prevents sharing across tasks in a
            # persistent store.
            session = self.sessions.get_or_create(state.task_id, self.name, engine.name)
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
        caller should ask the selector for a replacement engine.

        - ``TransientError`` retries the same session up to ``retry_budget``
          times, with exponential backoff between attempts. If the budget
          exhausts, the engine is treated as blocked and the caller switches.
        - ``NudgeRequired`` reissues the turn with a nudge-variant prompt
          and does NOT count against ``retry_budget``. Nudges use their own
          ``nudge_budget``. Exhaustion produces a ``Crash``.
        - ``EngineBlockedError`` is propagated for an immediate engine
          switch, no retry.
        - ``UnrecoverableError`` is escalated as a ``Crash``.
        """
        last_exc: Exception | None = None
        attempts_used = 0
        nudges_used = 0
        current_prompt = prompt

        while attempts_used < self.retry_budget:
            if attempts_used > 0 and self.retry_backoff_seconds > 0:
                delay = self.retry_backoff_seconds * (self.retry_backoff_multiplier ** (attempts_used - 1))
                self._sleep(delay)
            try:
                verdict = engine.run_turn(session, current_prompt, state)
                self.sessions.persist(state.task_id, self.name, engine.name, session)
                return self.verdict_to_event(verdict)
            except NudgeRequired as exc:
                if nudges_used >= self.nudge_budget:
                    return Crash(
                        exc_type="NudgeBudgetExhausted",
                        message=(
                            f"agent did not submit a verdict via `litehive agent report` after being nudged: {exc}"
                        ),
                    )
                nudges_used += 1
                current_prompt = self.build_nudge_prompt(state, current_prompt)
                # Nudge reuses the same session (so the adapter emits
                # --continue) but does not consume a retry attempt.
                continue
            except TransientError as exc:
                if exc.failure_kind not in self.retry_on:
                    return EngineBlockedError(
                        f"{engine.name} failed with non-retryable execution kind {exc.failure_kind or 'unknown'}: {exc}"
                    )
                last_exc = exc
                attempts_used += 1
                continue
            except EngineBlockedError as exc:
                return exc
            except UnrecoverableError as exc:
                return Crash(exc_type=type(exc).__name__, message=str(exc))
        return EngineBlockedError(f"retry budget ({self.retry_budget}) exhausted on {engine.name}: {last_exc}")

    def verdict_to_event(self, verdict: AgentVerdict) -> Event:
        """Translate the adapter-level ``AgentVerdict`` into the state-machine Event vocabulary so the runner stays oblivious to engine response shapes."""
        outcome = verdict.outcome.lower()
        if outcome == "pass":
            return Pass(metadata=dict(verdict.metadata or {}))
        if outcome == "reject":
            metadata = dict(verdict.metadata or {})
            classification = verdict.classification or _metadata_classification(metadata)
            if classification is not None:
                metadata.setdefault("verdict_classification", classification)
            return Reject(
                source=verdict.source,
                reason=verdict.reason,
                classification=classification,
                metadata=metadata,
            )
        if outcome == "blocked":
            return Blocked(reason=verdict.reason)
        return Crash(exc_type="UnknownVerdict", message=verdict.outcome)


def _metadata_classification(metadata: dict[str, Any]) -> str | None:
    """Recover a verdict classification that adapters tucked into ``metadata`` instead of the dedicated field, so older engine integrations still produce typed Reject events."""
    for key in ("verdict_classification", "classification"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
