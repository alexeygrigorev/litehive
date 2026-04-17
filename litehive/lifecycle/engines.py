"""Concrete engine selector + heru-backed engine adapter for v2.

Two things live here:

1. ``ConfigBackedEngineSelector`` — reads the shared engine/model selection
   policy and returns the first eligible engine that isn't frozen and isn't
   in the caller's ``excluded`` set.
2. ``HeruEngineAdapter`` — wraps a heru engine adapter so it matches the v2
   ``Engine`` protocol (``run_turn(session, prompt, state) → AgentVerdict``)
   and translates heru exceptions into the error taxonomy.

The selector never imports heru directly; it receives an ``engine_factory``
callable that turns an engine name into an ``Engine`` instance. Tests can
pass a fake factory; production passes a ``HeruEngineFactory`` bound to a
workspace root and session context.
"""

from pathlib import Path
from typing import Callable

from litehive.config.engine_models import (
    is_engine_frozen,
    resolve_engine_attempt_order,
    select_engine,
)
from litehive.config.model import LitehiveConfig
from litehive.state.records import get_task

from .nodes.agent import (
    Engine,
)
from .persistence import TaskState
from .types import NodeName


EngineFactory = Callable[[str], Engine]


class ConfigBackedEngineSelector:
    """``EngineSelector`` driven by ``LitehiveConfig``.

    Resolves the task's next engine/model using the shared selection logic,
    then materializes the corresponding engine instance.

    When task context is unavailable, it falls back to the historical
    config-only behavior and simply walks ``config.engine_preference``.

    The engine instance itself is built via the injected
    ``engine_factory``. Returns ``None`` if every candidate is excluded
    or frozen — the AgentNode then escalates with
    ``Crash(AllEnginesExhausted)``.
    """

    def __init__(
        self,
        config: LitehiveConfig,
        engine_factory: EngineFactory,
        *,
        workspace_root: Path | None = None,
        engine_override: str | None = None,
        model_override: str | None = None,
    ) -> None:
        self.config = config
        self.engine_factory = engine_factory
        self.workspace_root = workspace_root.resolve() if workspace_root is not None else None
        self.engine_override = engine_override
        self.model_override = model_override

    def _fallback_select(self, excluded: frozenset[str]) -> Engine | None:
        for engine_name in self.config.engine_preference:
            if engine_name in excluded:
                continue
            if is_engine_frozen(self.config, engine_name):
                continue
            return self.engine_factory(engine_name)
        return None

    def select(
        self,
        state: TaskState,
        node_name: NodeName,
        excluded: frozenset[str],
    ) -> Engine | None:
        del node_name
        if self.workspace_root is None or not getattr(state, "task_id", None):
            return self._fallback_select(excluded)

        task = get_task(self.workspace_root, state.task_id)
        if task is None:
            return self._fallback_select(excluded)

        candidate_engine_names = [
            engine_name
            for engine_name in resolve_engine_attempt_order(
                task,
                self.config,
                engine_override=self.engine_override,
            )
            if engine_name not in excluded
        ]
        if not candidate_engine_names:
            return None

        selection = select_engine(
            self.workspace_root,
            task,
            self.config,
            model_override=self.model_override,
            engine_names=candidate_engine_names,
        )
        if selection.engine_name is None:
            return None

        engine = self.engine_factory(selection.engine_name)
        with_model = getattr(engine, "with_model", None)
        if callable(with_model):
            configured_engine = with_model(selection.model_name)
            if configured_engine is not None:
                return configured_engine
        return engine


__all__ = [
    "ConfigBackedEngineSelector",
    "EngineFactory",
]
