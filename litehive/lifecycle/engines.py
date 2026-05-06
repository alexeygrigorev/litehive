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

from typing import Callable, cast

from litehive.config.engine_models import (
    select_engine_for_workspace,
)
from litehive.config.model import LitehiveConfig
from litehive.domain.task import TaskRecord
from litehive.state.records import get_task
from litehive.workspace import Workspace

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
        workspace: Workspace,
        engine_override: str | None = None,
        model_override: str | None = None,
        check_quota: bool = True,
    ) -> None:
        """
        Bind the selector to a workspace plus the caller's overrides.

        Captured here so each ``select`` call only needs the AgentNode's
        per-turn context (state, node, excluded set); the workspace
        config and CLI overrides do not change between turns and
        re-resolving them every turn would just be repeated work.
        """
        self.config = config
        self.engine_factory = engine_factory
        self.workspace = workspace
        self.workspace_root = workspace.root
        self.engine_override = engine_override
        self.model_override = model_override
        self.check_quota = check_quota

    def _selection_task(self, state: TaskState, node_name: PipelineState) -> TaskRecord | None:
        """
        Return the persisted task or a synthetic stand-in.

        Engine selection scores the candidate engines against the
        task; some lifecycle nodes run before a real task row exists
        (workspace-level recovery, for example), so we mint a synthetic
        ``TaskRecord`` rather than skip selection entirely on those
        paths.
        """
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
        """
        Pick the next engine to run this turn on, or ``None`` when no
        engine is eligible.

        Implements the ``EngineSelector`` protocol; AgentNode calls
        this before every turn so each pick honours CLI overrides,
        freezes, quota, and the AgentNode's ``excluded`` set of engines
        that have already crashed this run.
        """
        task = self._selection_task(state, node_name)
        if task is None:
            return None

        if self.check_quota:
            selection = select_engine_for_workspace(
                self.workspace,
                task,
                self.config,
                engine_override=self.engine_override,
                model_override=self.model_override,
                excluded_engine_names=excluded,
            )
        else:
            selection = select_engine_for_workspace(
                self.workspace,
                task,
                self.config,
                engine_override=self.engine_override,
                model_override=self.model_override,
                excluded_engine_names=excluded,
                check_quota=False,
            )
        if selection.engine_name is None:
            return None

        engine = self.engine_factory(selection.engine_name)
        with_model = getattr(engine, "with_model", None)
        if callable(with_model):
            configured_engine = with_model(selection.model_name)
            if configured_engine is not None:
                return cast(Engine, configured_engine)
        return engine


__all__ = [
    "ConfigBackedEngineSelector",
    "EngineFactory",
]
