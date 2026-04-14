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

from typing import Callable

from litehive.config.engine_models import is_engine_frozen
from litehive.config.model import LitehiveConfig

from .nodes.agent import (
    Engine,
)
from .persistence import TaskState
from .types import NodeName


EngineFactory = Callable[[str], Engine]


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
        for engine_name in self.config.engine_preference:
            if engine_name in excluded:
                continue
            if is_engine_frozen(self.config, engine_name):
                continue
            return self.engine_factory(engine_name)
        return None


__all__ = [
    "ConfigBackedEngineSelector",
    "EngineFactory",
]
