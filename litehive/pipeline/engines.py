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
from typing import Callable

from litehive.config import LitehiveConfig

from .nodes.agent import (
    Engine,
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


__all__ = [
    "ConfigBackedEngineSelector",
    "EngineFactory",
]
