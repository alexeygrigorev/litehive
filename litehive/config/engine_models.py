"""
Engine and model resolution for task execution.

Owns the precedence rules that pick which engine and model run a
given stage: task plan, workspace preference, freezes, quota
probes, and per-stage role mapping. Also persists quota-driven
freezes through the audited runtime-settings store so the same
freeze map drives both selection and operator-facing displays.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast

from heru import get_engine
from litehive.domain.common import TaskStage
from heru.quota import (
    UsageStatus,
    check_claude_quota,
    check_codex_quota,
    check_copilot_quota,
    check_zai_quota,
    preferred_reset_at,
)
from litehive.config.model import LitehiveConfig
from litehive.config.runtime_settings import clear_engine_freeze, set_engine_freeze
from litehive.domain.task import TaskRecord
from litehive.workspace import Workspace


def _engine_attempt_order(initial_engine_names: list[str], engine_preference: list[str]) -> list[str]:
    """
    Build the canonical engine fallback chain.

    Concatenates the task's initial engine list with the workspace
    preference and dedupes in first-seen order. Reused by
    :func:`select_engine_for_workspace` and
    :func:`resolve_engine_attempt_order` so the CLI preview and the
    actual execution path see the
    same chain — divergence here would let the operator preview
    one chain and watch a different one run.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for engine_name in list(initial_engine_names) + engine_preference:
        if engine_name in seen:
            continue
        seen.add(engine_name)
        ordered.append(engine_name)
    return ordered


@dataclass(frozen=True)
class EngineSkip:
    engine_name: str
    reason: str


@dataclass(frozen=True)
class EngineSelection:
    engine_name: str | None
    model_name: str | None
    engine_attempts: list[str]
    skipped: list[EngineSkip]
    blocked_reason: str | None = None


def _parse_datetime_utc(value: str | None) -> datetime | None:
    """
    Parse a stored freeze timestamp into a UTC-aware datetime.

    Accepts ISO 8601 (with optional trailing ``Z``) and the
    legacy ``YYYY-MM-DD`` form so older freeze entries still
    parse. Returns ``None`` for unparseable input so every
    freeze-window check can fall through cleanly without the
    storage format leaking into the caller.
    """
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(normalized, "%Y-%m-%d")
        except ValueError:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_engine_freeze_until(value: str | None) -> str | None:
    """
    Convert a CLI ``--until YYYY-MM-DD`` flag to the persisted ISO form.

    Returns ``None`` for unparseable input so the CLI can
    surface a single validation error rather than letting an
    invalid value land in the freeze map. The persisted form is
    the same UTC ISO with trailing ``Z`` that
    :func:`_parse_datetime_utc` expects on read.
    """
    if value is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dedupe_engine_names(engine_names: list[str]) -> list[str]:
    """
    Preserve first-seen order while dropping duplicates.

    Called by :func:`select_engine_for_workspace` when the caller hands in an
    explicit engine list. Callers can be sloppy about dedup
    (e.g. concat'ing overrides with a default) without breaking
    the attempt loop or producing a misleading "tried engine X
    twice" diagnostic.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for engine_name in engine_names:
        if engine_name in seen:
            continue
        seen.add(engine_name)
        ordered.append(engine_name)
    return ordered


def is_engine_frozen(config: LitehiveConfig, engine_name: str) -> bool:
    """
    Report whether an engine is currently frozen.

    Frozen means the freeze datetime is still in the future; an
    expired freeze is treated as not-frozen so engine selection
    self-cleans on the next pass without an explicit sweeper.
    """
    freeze_dt = _parse_datetime_utc(config.engine_freeze.get(engine_name))
    if freeze_dt is None:
        return False
    return datetime.now(timezone.utc) < freeze_dt


def active_engine_freezes(config: LitehiveConfig) -> dict[str, datetime]:
    """
    Return currently-active freezes as ``{engine: freeze_utc_datetime}``.

    Used by status renderers and engine selection to decide which
    engines are off the table right now. Drops expired entries
    silently because an expired freeze should not appear "active"
    to operator-facing output.
    """
    now = datetime.now(timezone.utc)
    result: dict[str, datetime] = {}
    for engine_name, freeze_str in config.engine_freeze.items():
        freeze_dt = _parse_datetime_utc(freeze_str)
        if freeze_dt is None:
            continue
        if now < freeze_dt:
            result[engine_name] = freeze_dt
    return result


def persist_engine_freeze_iso_for_workspace(
    workspace: Workspace,
    engine_name: str,
    freeze_iso: str,
    actor: str = "system",
    source: str = "runtime",
    reason: str | None = None,
) -> None:
    """
    Write a freeze entry through an injected workspace.

    Called by the CLI ``engine freeze`` command and by
    quota-driven freezes inside engine selection. Both paths
    funnel through here so audit rows for an operator-typed
    freeze and a quota-detected freeze share the same shape and
    can be compared directly.
    """
    if reason:
        context = {"reason": reason}
    else:
        context = None
    set_engine_freeze(
        workspace,
        engine_name=engine_name,
        freeze_iso=freeze_iso,
        actor=actor,
        source=source,
        context=context,
    )


def clear_persisted_engine_freeze_for_workspace(
    workspace: Workspace,
    engine_name: str,
    actor: str = "system",
    source: str = "runtime",
    reason: str | None = None,
) -> bool:
    """
    Remove a freeze entry through an injected workspace.

    Returns whether anything actually changed so callers can
    avoid emitting "unfroze nothing" log lines. Called by the
    CLI ``engine unfreeze`` command and by engine selection when
    a previously-frozen engine's freeze window has expired —
    same audit path as the freeze write so the audit log
    captures both directions.
    """
    if reason:
        context = {"reason": reason}
    else:
        context = None
    return clear_engine_freeze(
        workspace,
        engine_name=engine_name,
        actor=actor,
        source=source,
        context=context,
    ).changed


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
    selection pass. Called by :func:`select_engine_for_workspace` when an
    engine returns ``limit_reached`` with a reset time; mirroring
    on ``LitehiveConfig`` keeps subsequent selection within the
    same call seeing the freeze without re-reading the database.
    """
    freeze_iso = freeze_until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if config.engine_freeze.get(engine_name) == freeze_iso:
        return
    persist_engine_freeze_iso_for_workspace(
        workspace,
        engine_name=engine_name,
        freeze_iso=freeze_iso,
        actor="system",
        source="quota",
    )
    config.engine_freeze[engine_name] = freeze_iso


def _clear_engine_freeze(workspace: Workspace, config: LitehiveConfig, engine_name: str) -> None:
    """
    Drop a freeze entry from both the audited store and the live config.

    Called by :func:`select_engine_for_workspace` when a previously-frozen
    engine's window has lapsed and the quota check now passes;
    the freeze map self-cleans during normal selection so we
    avoid maintaining a separate freeze sweeper. The audited
    delete keeps the operator able to reconstruct freeze
    history.
    """
    clear_persisted_engine_freeze_for_workspace(workspace, engine_name=engine_name, actor="system", source="quota")
    config.engine_freeze.pop(engine_name, None)


def _quota_checker(engine_name: str):
    """
    Return the heru ``check_*_quota`` callable for an engine.

    Returns ``None`` for engines without a quota probe (Gemini
    today). The dispatch table lets :func:`engine_quota_block`
    stay generic across engines instead of growing a chain of
    ``if engine_name == ...`` branches at the call site.
    """
    if engine_name == "codex":
        return check_codex_quota
    if engine_name == "claude":
        return check_claude_quota
    if engine_name == "copilot":
        return check_copilot_quota
    if engine_name in ("goz", "opencode"):
        return check_zai_quota
    return None


def _quota_status_uses_unified_shape(status: object) -> bool:
    """
    Detect heru's "unified" quota status object.

    Identified by the presence of both ``short_term`` and
    ``long_term`` windows. Lets :func:`_quota_block_reason`
    ignore legacy or error-shaped statuses that don't carry
    enough info to act on; returning False there pushes the
    selector toward "engine is fine".
    """
    return getattr(status, "short_term", None) is not None and getattr(status, "long_term", None) is not None


def _preferred_quota_reset_at(status: object) -> str | None:
    """
    Ask heru for the most informative reset timestamp on a quota status.

    Swallows ``AttributeError`` from older status shapes so a
    new heru release adding the function does not break Litehive
    while older releases without it still work. Called by
    :func:`_quota_block_reason` so the freeze message can include
    a "resets …" hint when available.
    """
    try:
        return preferred_reset_at(cast("UsageStatus", status), include_short_term_fallback=True)
    except AttributeError:
        return None


def _quota_block_reason(engine_name: str, status: object) -> tuple[str | None, str | None]:
    """
    Translate a heru quota status into a skip reason and reset-at pair.

    Returns ``(None, None)`` whenever the status is errored,
    legacy-shaped, or not actually limit-reached so
    :func:`engine_quota_block` falls through to "engine is fine"
    without a chain of negative branches at the call site.
    """
    if getattr(status, "error", None) is not None:
        return None, None
    if not _quota_status_uses_unified_shape(status):
        return None, None
    if not bool(getattr(status, "limit_reached", False)):
        return None, None
    reset_at = _preferred_quota_reset_at(status)
    if reset_at:
        reset_suffix = f", resets {reset_at}"
    else:
        reset_suffix = ""
    return f"{engine_name} usage limit reached{reset_suffix}", reset_at


def engine_quota_block(
    engine_name: str,
) -> tuple[str | None, datetime | None]:
    """
    Probe the engine's vendor quota and report whether to skip it.

    Returns ``(skip_reason, freeze_until)`` when the engine is
    currently rate-limited; ``(None, None)`` when it is fine or
    has no probe. Called by the engine-selection loop so
    quota-blocked engines are skipped and auto-frozen until the
    reset time, without having to re-probe on the next stage.
    """
    checker = _quota_checker(engine_name)
    if checker is None:
        return None, None
    status = checker()
    reason, reset_at = _quota_block_reason(engine_name, status)
    return reason, _parse_datetime_utc(reset_at)


def select_engine_for_workspace(
    workspace: Workspace,
    task: TaskRecord,
    config: LitehiveConfig,
    engine_override: str | None = None,
    model_override: str | None = None,
    engine_names: list[str] | None = None,
    excluded_engine_names: Collection[str] = (),
    require_available: bool = False,
    check_quota: bool = True,
) -> EngineSelection:
    """
    Pick the engine and model the next stage will use from an injected workspace.

    Walks the task's engine plan plus the workspace preference
    list, skipping frozen, unavailable, and quota-exhausted
    engines, and returns the first usable ``(engine, model)``
    along with attempt/skip diagnostics so the operator can see
    *why* an engine was bypassed. Called by the runtime when a
    task transitions into an agent-driven stage and by the
    recovery agent when picking a fallback engine.
    """
    excluded = set(excluded_engine_names)
    if engine_names is not None:
        order = [engine_name for engine_name in _dedupe_engine_names(engine_names) if engine_name not in excluded]
    else:
        plan = resolve_engine_plan(
            task,
            config,
            engine_override=engine_override,
        )
        order = [
            engine_name
            for engine_name in _engine_attempt_order(plan, config.engine_preference)
            if engine_name not in excluded
        ]
    frozen_engines = active_engine_freezes(config)
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
        if require_available:
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
            engine_name not in frozen_engines and _parse_datetime_utc(config.engine_freeze.get(engine_name)) is not None
        )
        if check_quota:
            quota_reason, freeze_until = engine_quota_block(engine_name)
        else:
            quota_reason, freeze_until = (None, None)
        if quota_reason is not None:
            if freeze_until is not None:
                _persist_engine_freeze(workspace, config, engine_name=engine_name, freeze_until=freeze_until)
            elif expired_freeze:
                _clear_engine_freeze(workspace, config, engine_name=engine_name)
            skipped.append(EngineSkip(engine_name=engine_name, reason=quota_reason))
            continue
        if expired_freeze:
            _clear_engine_freeze(workspace, config, engine_name=engine_name)
        return EngineSelection(
            engine_name=engine_name,
            model_name=resolve_model(
                task,
                config,
                engine_name=engine_name,
                model_override=model_override,
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


def workspace_model_for_engine(config: LitehiveConfig, engine_name: str) -> str | None:
    """
    Return the workspace-level default model pinned for an engine.

    The bottom rung of the model-resolution ladder under
    task-level and CLI overrides. Returns ``None`` when the
    workspace has not pinned one so :func:`resolve_model` can
    fall through to the engine adapter's own default.
    """
    if engine_name == "codex":
        return config.codex_model
    if engine_name == "opencode":
        return config.opencode_model
    if engine_name == "goz":
        return config.goz_model
    if engine_name == "gemini":
        return config.gemini_model
    if engine_name == "copilot":
        return config.copilot_model
    if engine_name == "claude":
        return config.claude_model
    return None


def resolve_model(
    task: TaskRecord,
    config: LitehiveConfig,
    engine_name: str,
    model_override: str | None = None,
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
    if model_override is not None:
        return model_override
    if task.model is not None:
        return task.model
    return workspace_model_for_engine(config, engine_name)


def resolve_engine_name(
    task: TaskRecord,
    config: LitehiveConfig,
    engine_override: str | None = None,
) -> str:
    """
    Return just the first engine the task would attempt.

    Used by status and diagnostic surfaces that want to display
    the "primary" engine without running the full attempt loop;
    the full loop additionally consults frozen/unavailable state
    and is too heavy for status rendering.
    """
    return resolve_engine_plan(task, config, engine_override=engine_override)[0]


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
    order = _engine_attempt_order(
        resolve_engine_plan(task, config, engine_override=engine_override),
        config.engine_preference,
    )
    if config.engine_freeze:
        order = [e for e in order if not is_engine_frozen(config, e)]
    return order


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


def _is_recovery_run(task: TaskRecord) -> bool:
    """
    Detect whether a task is currently in a recovery posture.

    Recovery posture means an active interruption or a
    flagged/interrupted last outcome. Consumed by
    :func:`_role_for_stage` so engine selection picks the
    recovery role's preference list instead of the normal
    stage-owner role; the recovery agent is supposed to lead
    after a flag, not the SWE/QA/reviewer that originally ran.
    """
    return task.runtime.execution.interruption is not None or task.runtime.pipeline.last_outcome.kind in {
        "flagged",
        "interrupted",
    }


_RECOVERY_HIJACKABLE_STAGES = frozenset({TaskStage.IMPLEMENTING, TaskStage.TESTING, TaskStage.ACCEPTING})

# Engine selection only knows about the four agent-driven stages; other
# stages (commit_to_git, recovering, …) fall through to ``swe`` as the
# default selector role. Keeping this map separate from
# ``TaskStage.owner_role`` so the engine-resolution fallback stays
# stable when the canonical stage→role mapping changes.
_ENGINE_SELECTION_ROLE_BY_STAGE: dict[TaskStage, str] = {
    TaskStage.GROOMING: "planner",
    TaskStage.IMPLEMENTING: "swe",
    TaskStage.TESTING: "qa",
    TaskStage.ACCEPTING: "reviewer",
}


def _role_for_stage(stage: str, task: TaskRecord | None = None) -> str:
    """
    Map a pipeline stage to the engine-selection role.

    Returns ``planner``/``swe``/``qa``/``reviewer``/``recovery``,
    redirecting agent-driven stages to ``recovery`` when the
    task is mid-recovery. Module-private because the canonical
    stage->role mapping lives on :attr:`TaskStage.owner_role`
    and this one is intentionally narrower — engine selection
    only picks for the four agent-driven stages, every other
    stage falls back to ``swe``.
    """
    if task is not None and stage in _RECOVERY_HIJACKABLE_STAGES and _is_recovery_run(task):
        return "recovery"
    try:
        return _ENGINE_SELECTION_ROLE_BY_STAGE.get(TaskStage(stage), "swe")
    except ValueError:
        return "swe"
