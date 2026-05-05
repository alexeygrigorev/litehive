"""Concrete engine selector + heru-backed engine adapter.

Two things live here:

1. ``ConfigBackedEngineSelector`` — reads the shared engine/model selection
   policy and returns the first eligible engine that isn't frozen and isn't
   in the caller's ``excluded`` set.
2. ``HeruEngineAdapter`` — wraps a heru engine adapter so it matches the
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
    select_engine,
)
from litehive.config.model import LitehiveConfig
from litehive.domain.task import TaskRecord
from litehive.state.records import get_task

from .nodes.agent import (
    Engine,
)
from litehive.domain.common import PipelineState
from .persistence import TaskState


EngineFactory = Callable[[str], Engine]


class ConfigBackedEngineSelector:
    """``EngineSelector`` driven by ``LitehiveConfig``.

    Resolves the task's next engine/model using the shared selection logic,
    then materializes the corresponding engine instance.

    When a persisted task record is unavailable, it synthesizes minimal
    task context so selection still flows through the shared quota-aware
    selector.

    The engine instance itself is built via the injected
    ``engine_factory``. Returns ``None`` if every candidate is excluded
    or frozen — the AgentNode then escalates with
    ``Crash(AllEnginesExhausted)``.
    """

    def __init__(
        self,
        config: LitehiveConfig,
        engine_factory: EngineFactory,
        workspace_root: Path | None = None,
        engine_override: str | None = None,
        model_override: str | None = None,
        check_quota: bool = True,
    ) -> None:
        self.config = config
        self.engine_factory = engine_factory
        if workspace_root is not None:
            self.workspace_root = workspace_root.resolve()
        else:
            self.workspace_root = None
        self.engine_override = engine_override
        self.model_override = model_override
        self.check_quota = check_quota

    def _selection_task(self, state: TaskState, node_name: PipelineState) -> TaskRecord | None:
        if self.workspace_root is None:
            return None
        if getattr(state, "task_id", None):
            task = get_task(self.workspace_root, state.task_id)
            if task is not None:
                return task
        return TaskRecord(
            id=getattr(state, "task_id", None) or "T-runtime-selection",
            slug="runtime-engine-selection",
            title="Runtime engine selection",
            pipeline_mode=state.pipeline_mode.value,
            status="in_progress",
            pipeline_status=node_name,
        )

    def select(
        self,
        state: TaskState,
        node_name: PipelineState,
        excluded: frozenset[str],
    ) -> Engine | None:
        """Implement the `EngineSelector` protocol — invoked by every AgentNode before each turn to obtain the next engine instance, honouring CLI overrides, freezes, quota, and the AgentNode's `excluded` set of engines that have already crashed this run."""
        task = self._selection_task(state, node_name)
        if task is None:
            return None

        selection_kwargs = {
            "engine_override": self.engine_override,
            "model_override": self.model_override,
            "excluded_engine_names": excluded,
        }
        if not self.check_quota:
            selection_kwargs["check_quota"] = False
        selection = select_engine(self.workspace_root, task, self.config, **selection_kwargs)
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
