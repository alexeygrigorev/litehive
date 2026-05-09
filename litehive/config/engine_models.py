"""
Engine and model resolution for task execution.

Owns the precedence rules that pick which engine and model run a
given stage: task plan, workspace preference, freezes, and quota
probes. Also persists quota-driven
freezes through the audited runtime-settings store so the same
freeze map drives both selection and operator-facing displays.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone

from heru import get_engine
from litehive.config.engine_freezes import (
    active_engine_freezes,
    is_engine_frozen,
)
from litehive.config.engine_quota import EngineQuotaBlock, engine_quota_block
from litehive.config.model import LitehiveConfig
from litehive.config.runtime_settings import RuntimeSettingContext, clear_engine_freeze, set_engine_freeze
from litehive.config.runtime_settings import set_default_engine as set_default_engine_setting
from litehive.config.runtime_settings import set_engine_preference as set_engine_preference_setting
from litehive.config.time_parsing import parse_engine_freeze_until as parse_engine_freeze_until
from litehive.config.time_parsing import parse_utc_datetime
from litehive.domain.task import TaskRecord
from litehive.workspace import Workspace


@dataclass(frozen=True)
class EngineSkip:
    """
    Diagnostic entry for one candidate engine that selection bypassed.

    Attributes:
        engine_name: Engine identifier from the attempted candidate list.
        reason: Operator-facing reason the engine was not selected.
    """

    engine_name: str
    reason: str


@dataclass(frozen=True)
class EngineSelection:
    """
    Result of resolving an executable engine/model pair for a task.

    Attributes:
        engine_name: Selected engine, or ``None`` when no candidate can run.
        model_name: Selected model for engines that support model overrides.
        engine_attempts: Candidate engines considered after freeze filtering.
        skipped: Candidate engines rejected during availability/quota checks.
        blocked_reason: Summary reason when no engine can be selected; empty
            for successful selections.
    """

    engine_name: str | None
    model_name: str | None
    engine_attempts: list[str]
    skipped: list[EngineSkip]
    blocked_reason: str = ""


@dataclass(frozen=True)
class EngineSelectionRequest:
    """
    Optional controls for one engine-selection pass.

    Attributes:
        engine_override: Operator-selected engine that replaces the
            task/config default engine for this pass.
        requested_model_name: Operator-selected model passed to engines that
            support model overrides.
        engine_names: Explicit candidate engine list, used by recovery
            and tests that need to bypass task/config planning.
        excluded_engine_names: Engines already ruled out by the caller
            during this run.
        require_available: Whether to ask Heru if each candidate is
            available before selecting it.
        check_quota: Whether to run quota checks and persist quota-driven
            freezes.
    """

    engine_override: str | None = None
    requested_model_name: str | None = None
    engine_names: list[str] | None = None
    excluded_engine_names: Collection[str] = ()
    require_available: bool = False
    check_quota: bool = True


class EngineRoutingPolicy:
    """
    Workspace-bound engine routing and engine-control policy.
    """

    def __init__(self, workspace: Workspace, config: LitehiveConfig) -> None:
        self.workspace = workspace
        self.config = config

    def select(self, task: TaskRecord, request: EngineSelectionRequest | None = None) -> EngineSelection:
        if request is None:
            selection_request = EngineSelectionRequest()
        else:
            selection_request = request
        order = _candidate_engine_order(task, self.config, selection_request)
        frozen_engines = active_engine_freezes(self.config)
        attempts = [engine_name for engine_name in order if engine_name not in frozen_engines]
        skipped: list[EngineSkip] = []
        if not attempts and order and all(engine_name in frozen_engines for engine_name in order):
            return EngineSelection(
                engine_name=None,
                model_name=None,
                engine_attempts=[],
                skipped=[],
                blocked_reason="all candidate engines are frozen",
            )
        for engine_name in attempts:
            if selection_request.require_available:
                try:
                    if not get_engine(engine_name).is_available():
                        skipped.append(EngineSkip(engine_name=engine_name, reason=f"{engine_name} unavailable"))
                        continue
                except (OSError, RuntimeError, ValueError) as exc:
                    skipped.append(
                        EngineSkip(
                            engine_name=engine_name,
                            reason=f"{engine_name} availability check failed ({type(exc).__name__}: {exc})",
                        )
                    )
                    continue
            expired_freeze = (
                engine_name not in frozen_engines
                and parse_utc_datetime(self.config.engine_freeze.get(engine_name)) is not None
            )
            if selection_request.check_quota:
                quota_block = self.quota_status(engine_name)
            else:
                quota_block = None
            if quota_block is not None:
                if quota_block.freeze_until is not None:
                    _persist_engine_freeze(
                        self.workspace,
                        self.config,
                        engine_name=engine_name,
                        freeze_until=quota_block.freeze_until,
                    )
                elif expired_freeze:
                    _clear_engine_freeze(self.workspace, self.config, engine_name)
                skipped.append(EngineSkip(engine_name=engine_name, reason=quota_block.reason))
                continue
            if expired_freeze:
                _clear_engine_freeze(self.workspace, self.config, engine_name)
            return EngineSelection(
                engine_name=engine_name,
                model_name=self.resolve_model_override(
                    task,
                    engine_name,
                    requested_model_name=selection_request.requested_model_name,
                ),
                engine_attempts=attempts,
                skipped=skipped,
            )
        if skipped:
            blocked_reason = skipped[-1].reason
        else:
            blocked_reason = "no eligible engine available"
        return EngineSelection(
            engine_name=None,
            model_name=None,
            engine_attempts=attempts,
            skipped=skipped,
            blocked_reason=blocked_reason,
        )

    def resolve_engine_name(self, task: TaskRecord, engine_override: str | None = None) -> str:
        initial_engine_names = resolve_engine_plan(task, self.config, engine_override=engine_override)
        attempt_order = _unfrozen_engine_attempt_order(self.config.engine_attempt_order(initial_engine_names), self.config)
        if attempt_order:
            return attempt_order[0]
        return initial_engine_names[0]

    def resolve_model_override(
        self,
        task: TaskRecord,
        engine_name: str,
        requested_model_name: str | None = None,
    ) -> str | None:
        if not get_engine(engine_name).capabilities.supports_model_override:
            return None
        if requested_model_name is not None:
            return requested_model_name
        if task.model is not None:
            return task.model
        return self.config.model_for_engine(engine_name)

    def resolve_recovery_engine(self, task: TaskRecord) -> tuple[str, str | None]:
        engine_override = None
        if self.config.recovery_engine and self.config.recovery_engine != "auto":
            engine_override = self.config.recovery_engine
        selection = self.select(
            task,
            EngineSelectionRequest(engine_override=engine_override, require_available=True),
        )
        if selection.engine_name is None:
            raise RuntimeError(selection.blocked_reason)
        return selection.engine_name, selection.model_name

    def freeze(self, engine: str, until: str, reason: str | None = None) -> None:
        set_engine_freeze(
            self.workspace,
            engine_name=engine,
            freeze_iso=until,
            actor="operator",
            source="cli",
            context=_reason_context(reason),
        )
        self.config.engine_freeze[engine] = until

    def unfreeze(self, engine: str, reason: str | None = None) -> bool:
        changed = clear_engine_freeze(
            self.workspace,
            engine_name=engine,
            actor="operator",
            source="cli",
            context=_reason_context(reason),
        ).changed
        if changed:
            self.config.engine_freeze.pop(engine, None)
        return changed

    def set_default(self, engine: str, reason: str | None = None) -> None:
        context = _reason_context(reason)
        set_default_engine_setting(
            self.workspace,
            engine_name=engine,
            actor="operator",
            source="cli",
            context=context,
        )
        self.config.default_engine = engine

    def set_preference(self, order: list[str], reason: str | None = None) -> None:
        context = _reason_context(reason)
        set_engine_preference_setting(
            self.workspace,
            engines=order,
            actor="operator",
            source="cli",
            context=context,
        )
        self.config.engine_preference = list(order)

    def quota_status(self, engine: str) -> EngineQuotaBlock | None:
        return engine_quota_block(engine)

    def clear_expired_freezes(self) -> None:
        for engine_name, freeze_until in list(self.config.engine_freeze.items()):
            if parse_utc_datetime(freeze_until) is None:
                continue
            if is_engine_frozen(self.config, engine_name):
                continue
            _clear_engine_freeze(self.workspace, self.config, engine_name)

def _reason_context(reason: str | None) -> RuntimeSettingContext | None:
    if reason is None:
        return None
    return {"reason": reason}


def _candidate_engine_order(
    task: TaskRecord,
    config: LitehiveConfig,
    request: EngineSelectionRequest,
) -> list[str]:
    """
    Build the ordered candidate engines before freeze/quota filtering.
    """
    excluded = set(request.excluded_engine_names)
    if request.engine_names is not None:
        return [engine_name for engine_name in request.engine_names if engine_name not in excluded]
    plan = resolve_engine_plan(
        task,
        config,
        engine_override=request.engine_override,
    )
    return [engine_name for engine_name in config.engine_attempt_order(plan) if engine_name not in excluded]


def _persist_engine_freeze(
    workspace: Workspace,
    config: LitehiveConfig,
    engine_name: str,
    freeze_until: datetime,
) -> None:
    """
    Persist a quota-driven freeze and mirror it on the in-memory config.

    Skips the write when the new ISO value matches the existing
    one so we don't spam the audit log with no-op rows on every
    selection pass. Called by :meth:`EngineRoutingPolicy.select` when an
    engine returns ``limit_reached`` with a reset time; mirroring
    on ``LitehiveConfig`` keeps subsequent selection within the
    same call seeing the freeze without re-reading the database.
    """
    freeze_iso = freeze_until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if config.engine_freeze.get(engine_name) == freeze_iso:
        return
    set_engine_freeze(
        workspace,
        engine_name=engine_name,
        freeze_iso=freeze_iso,
        actor="system",
        source="quota",
        context=None,
    )
    config.engine_freeze[engine_name] = freeze_iso


def _clear_engine_freeze(workspace: Workspace, config: LitehiveConfig, engine_name: str) -> None:
    """
    Drop a freeze entry from both the audited store and the live config.

    Called by :meth:`EngineRoutingPolicy.select` when a previously-frozen
    engine's window has lapsed and the quota check now passes;
    the freeze map self-cleans during normal selection so we
    avoid maintaining a separate freeze sweeper. The audited
    delete keeps the operator able to reconstruct freeze
    history.
    """
    clear_engine_freeze(workspace, engine_name=engine_name, actor="system", source="quota", context=None)
    config.engine_freeze.pop(engine_name, None)


def resolve_model(
    task: TaskRecord,
    config: LitehiveConfig,
    engine_name: str,
    requested_model_name: str | None,
) -> str | None:
    """
    Pick the model name for a chosen engine.

    Precedence: CLI override > ``task.model`` > workspace
    default. Returns ``None`` when the engine does not support
    model selection at all so the engine adapter does not get
    handed a value it would have to ignore. Called by engine
    selection so the runner hands the adapter the right model in
    one place.
    """
    if not get_engine(engine_name).capabilities.supports_model_override:
        return None
    if requested_model_name is not None:
        return requested_model_name
    if task.model is not None:
        return task.model
    return config.model_for_engine(engine_name)


def resolve_engine_name(
    task: TaskRecord,
    config: LitehiveConfig,
    engine_override: str | None = None,
) -> str:
    """
    Return the first unfrozen engine the task would attempt.

    Used by status and diagnostic surfaces that want to display
    the next engine without running the full availability/quota
    loop. If every candidate is frozen, returns the initial planned
    engine so callers that require a string still get the task's
    configured primary engine.
    """
    initial_engine_names = resolve_engine_plan(task, config, engine_override=engine_override)
    attempt_order = _unfrozen_engine_attempt_order(config.engine_attempt_order(initial_engine_names), config)
    if attempt_order:
        return attempt_order[0]
    return initial_engine_names[0]


def resolve_engine_attempt_order(
    task: TaskRecord,
    config: LitehiveConfig,
    engine_override: str | None = None,
) -> list[str]:
    """
    Produce the engine attempt sequence with freezes removed.

    Returns the order a task would walk if no overrides or quota
    blocks fired during the run. Called by the CLI ``engine``
    introspection commands so operators can preview the fallback
    chain without executing a stage; freezes are filtered up
    front so the preview reflects current state.
    """
    initial_engine_names = resolve_engine_plan(task, config, engine_override=engine_override)
    return _unfrozen_engine_attempt_order(config.engine_attempt_order(initial_engine_names), config)


def _unfrozen_engine_attempt_order(engine_names: list[str], config: LitehiveConfig) -> list[str]:
    """
    Filter active freezes from an already-ordered candidate list.
    """
    if not config.engine_freeze:
        return engine_names
    return [engine_name for engine_name in engine_names if not is_engine_frozen(config, engine_name)]


def resolve_engine_plan(
    task: TaskRecord,
    config: LitehiveConfig,
    engine_override: str | None = None,
) -> list[str]:
    """
    Compute the initial engine list before merging with workspace preferences.

    Honors an explicit override and any in-flight engine switch
    recorded for the current stage. The recorded switch is what
    makes a mid-stage ``engine switch`` decision stick across
    retries instead of getting overwritten by the workspace
    default — without it the operator's switch would silently
    revert on the next retry.
    """
    if engine_override is not None:
        return [engine_override]
    if (
        task.runtime.execution.last_engine_switch is not None
        and task.runtime.execution.last_engine_switch.stage == task.pipeline_status
    ):
        return [task.runtime.execution.last_engine_switch.to_engine]
    return [config.default_engine]


def resolve_task_retry_policy(task: TaskRecord, config: LitehiveConfig) -> int:
    """
    Return the per-task retry budget.

    Falls back to the workspace default when the task has not
    pinned its own. Called by the orchestrator to bound automatic
    retry attempts before a task is marked stuck — the budget
    decides when the recovery agent takes over from automatic
    retries.
    """
    if task.retry_policy.max_retries is not None:
        return task.retry_policy.max_retries
    return config.default_retry_limit


def _resolve_stage_retry_limit(task: TaskRecord, config: LitehiveConfig) -> int:
    """
    Return the per-stage retry budget.

    Falls back to the workspace default when the task has not
    pinned its own. Orchestrator-internal sibling of
    :func:`resolve_task_retry_policy` that bounds attempts within
    a single pipeline stage rather than across the whole task —
    necessary because a flaky single stage can otherwise burn the
    task-level budget on the same failure mode.
    """
    if task.retry_policy.stage_retry_limit is not None:
        return task.retry_policy.stage_retry_limit
    return config.default_stage_retry_limit


def resolve_task_rejection_loop_limit(task: TaskRecord, config: LitehiveConfig) -> int:
    """
    Return how many accept->reject->re-implement loops a task may run.

    Falls back to the workspace default when the task has not
    pinned its own. The limit caps cycles where the reviewer
    rejects, the SWE re-implements, and the loop comes back —
    without a cap the same disagreement could spin indefinitely.
    """
    if task.retry_policy.rejection_loop_limit is not None:
        return task.retry_policy.rejection_loop_limit
    return config.default_rejection_loop_limit
