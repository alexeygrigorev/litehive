"""Concrete engine selector + heru-backed engine adapter for v2.

Two things live here:

1. ``ConfigBackedEngineSelector`` — reads ``LitehiveConfig.engine_preference``
   and ``LitehiveConfig.engine_freeze``, and returns the first eligible
   engine that isn't frozen and isn't in the caller's ``excluded`` set.
2. ``HeruEngineAdapter`` — wraps a heru engine adapter so it matches the v2
   ``Engine`` protocol (``run_turn(session, prompt, state) → AgentVerdict``)
   and translates heru exceptions into the error taxonomy.

The selector never imports heru directly; it receives an ``engine_factory``
callable that turns an engine name into an ``Engine`` instance. Tests can
pass a fake factory; production passes a ``HeruEngineFactory`` bound to a
workspace root and session context.
"""

from datetime import UTC, datetime
from typing import Any, Callable

from litehive.config import LitehiveConfig

from .nodes.agent import (
    AgentVerdict,
    Engine,
    EngineOverloaded,
    ModelUnavailable,
    NudgeRequired,
    QuotaExceeded,
    TransientError,
    UnrecoverableError,
)
from .persistence import TaskState
from .types import NodeName


EngineFactory = Callable[[str], Engine]


def _parse_freeze(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _is_frozen(config: LitehiveConfig, engine_name: str, now: datetime | None = None) -> bool:
    freeze_dt = _parse_freeze(config.engine_freeze.get(engine_name))
    if freeze_dt is None:
        return False
    return (now or datetime.now(UTC)) < freeze_dt


class ConfigBackedEngineSelector:
    """``EngineSelector`` driven by ``LitehiveConfig``.

    Walks ``config.engine_preference`` in order and returns the first
    engine that is:

    - not in the caller's ``excluded`` set (engines ruled out by a
      previous ``EngineBlockedError`` during this node visit)
    - not currently frozen (``config.engine_freeze[name]`` is either
      missing or in the past)

    The engine instance itself is built via the injected
    ``engine_factory``. Returns ``None`` if every candidate is excluded
    or frozen — the AgentNode then escalates with
    ``Crash(AllEnginesExhausted)``.
    """

    def __init__(self, config: LitehiveConfig, engine_factory: EngineFactory) -> None:
        self.config = config
        self.engine_factory = engine_factory

    def select(
        self,
        state: TaskState,
        node_name: NodeName,
        excluded: frozenset[str],
    ) -> Engine | None:
        now = datetime.now(UTC)
        for engine_name in self.config.engine_preference:
            if engine_name in excluded:
                continue
            if _is_frozen(self.config, engine_name, now):
                continue
            return self.engine_factory(engine_name)
        return None


# ── Heru adapter ────────────────────────────────────────────────────────


# Errors raised by heru adapters that we want to treat as engine-blocked.
# These string names are matched against ``type(exc).__name__`` so we don't
# have to import heru here just to type-check the exception classes.
_QUOTA_EXCEPTION_NAMES = {
    "QuotaExceededError",
    "QuotaExhausted",
    "RateLimitError",
}
_OVERLOAD_EXCEPTION_NAMES = {
    "EngineOverloadedError",
    "ServiceOverloaded",
    "TooManyRequests",
}
_MODEL_UNAVAILABLE_NAMES = {
    "ModelUnavailableError",
    "ModelNotFound",
}
_TRANSIENT_EXCEPTION_NAMES = {
    "TransientError",
    "ConnectionError",
    "TimeoutError",
    "ReadTimeout",
    "WriteTimeout",
    "IncompleteRead",
}
_NUDGE_EXCEPTION_NAMES = {
    "MissingVerdictError",
    "NoReportSubmitted",
}


class HeruEngineAdapter:
    """Wraps a heru engine adapter as a ``Engine``.

    Provides the ``run_turn(session, prompt, state) → AgentVerdict`` contract
    by:

    1. Calling into the underlying ``heru`` adapter with the session id
       (if present, via ``--continue``).
    2. Translating heru's result into an ``AgentVerdict`` via the task's
       structured ``litehive report`` submission, read back from sqlite.
    3. Translating heru's exceptions into the error taxonomy
       (``TransientError`` / ``QuotaExceeded`` / ``EngineOverloaded`` /
       ``ModelUnavailable`` / ``NudgeRequired`` / ``UnrecoverableError``).

    The adapter is intentionally thin: the heavy lifting is still inside
    heru. This class just mediates the contract.

    Call sites inject a ``heru_runner`` callable that takes (engine_name,
    prompt, session_id, workspace_root) and returns a small result object
    the adapter can inspect. This indirection lets tests swap in a fake
    runner without touching heru itself.
    """

    def __init__(
        self,
        engine_name: str,
        *,
        heru_runner: Callable[..., Any],
        verdict_reader: Callable[[TaskState], AgentVerdict | None],
    ) -> None:
        self.name = engine_name
        self.heru_runner = heru_runner
        self.verdict_reader = verdict_reader

    def run_turn(self, session: Any, prompt: Any, state: TaskState) -> AgentVerdict:
        session_id = getattr(session, "engine_session_id", None)
        try:
            result = self.heru_runner(
                engine_name=self.name,
                prompt=prompt,
                session_id=session_id,
                state=state,
            )
        except Exception as exc:
            self._reraise_as_v2(exc)
            raise  # unreachable

        # Persist the new session id on the Session object
        new_session_id = getattr(result, "session_id", None) or session_id
        if hasattr(session, "engine_session_id"):
            session.engine_session_id = new_session_id
            session.turn_count = (getattr(session, "turn_count", 0) or 0) + 1

        # After the turn, check whether the agent actually submitted a
        # verdict via `litehive report`. If not, raise NudgeRequired so
        # AgentNode reissues the turn on this session with a nudge prompt.
        verdict = self.verdict_reader(state)
        if verdict is None:
            raise NudgeRequired(
                f"{self.name} finished turn without a litehive report submission"
            )
        return verdict

    @staticmethod
    def _reraise_as_v2(exc: Exception) -> None:
        exc_name = type(exc).__name__
        if exc_name in _NUDGE_EXCEPTION_NAMES:
            raise NudgeRequired(str(exc)) from exc
        if exc_name in _QUOTA_EXCEPTION_NAMES:
            raise QuotaExceeded(str(exc)) from exc
        if exc_name in _OVERLOAD_EXCEPTION_NAMES:
            raise EngineOverloaded(str(exc)) from exc
        if exc_name in _MODEL_UNAVAILABLE_NAMES:
            raise ModelUnavailable(str(exc)) from exc
        if exc_name in _TRANSIENT_EXCEPTION_NAMES:
            raise TransientError(str(exc)) from exc
        # Any other exception — treat as unrecoverable so the state machine
        # routes it through recovery.
        raise UnrecoverableError(f"{exc_name}: {exc}") from exc


__all__ = [
    "ConfigBackedEngineSelector",
    "EngineFactory",
    "HeruEngineAdapter",
]
