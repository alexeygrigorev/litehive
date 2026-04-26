"""Model and engine resolution."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from heru import get_engine
from heru.quota import (
    check_claude_quota,
    check_codex_quota,
    check_copilot_quota,
    check_zai_quota,
    preferred_reset_at,
)
from litehive.config.model import LitehiveConfig
from litehive.config.runtime_settings import clear_engine_freeze, set_engine_freeze
from litehive.domain.task import TaskRecord


def _engine_attempt_order(initial_engine_names: list[str], engine_preference: list[str]) -> list[str]:
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
    if value is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dedupe_engine_names(engine_names: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for engine_name in engine_names:
        if engine_name in seen:
            continue
        seen.add(engine_name)
        ordered.append(engine_name)
    return ordered


def is_engine_frozen(config: LitehiveConfig, engine_name: str) -> bool:
    """Return True if the engine is currently frozen (freeze datetime in the future)."""
    freeze_dt = _parse_datetime_utc(config.engine_freeze.get(engine_name))
    if freeze_dt is None:
        return False
    return datetime.now(timezone.utc) < freeze_dt


def active_engine_freezes(config: LitehiveConfig) -> dict[str, datetime]:
    """Return currently active freezes as {engine: freeze_utc_datetime}."""
    now = datetime.now(timezone.utc)
    result: dict[str, datetime] = {}
    for engine_name, freeze_str in config.engine_freeze.items():
        freeze_dt = _parse_datetime_utc(freeze_str)
        if freeze_dt is None:
            continue
        if now < freeze_dt:
            result[engine_name] = freeze_dt
    return result


def persist_engine_freeze_iso(
    root: Path,
    *,
    engine_name: str,
    freeze_iso: str,
    actor: str = "system",
    source: str = "runtime",
    reason: str | None = None,
) -> None:
    context = {"reason": reason} if reason else None
    set_engine_freeze(
        root,
        engine_name=engine_name,
        freeze_iso=freeze_iso,
        actor=actor,
        source=source,
        context=context,
    )


def clear_persisted_engine_freeze(
    root: Path,
    *,
    engine_name: str,
    actor: str = "system",
    source: str = "runtime",
    reason: str | None = None,
) -> bool:
    context = {"reason": reason} if reason else None
    return clear_engine_freeze(root, engine_name=engine_name, actor=actor, source=source, context=context).changed


def _persist_engine_freeze(
    root: Path,
    config: LitehiveConfig,
    *,
    engine_name: str,
    freeze_until: datetime,
) -> None:
    freeze_iso = freeze_until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if config.engine_freeze.get(engine_name) == freeze_iso:
        return
    persist_engine_freeze_iso(root, engine_name=engine_name, freeze_iso=freeze_iso, actor="system", source="quota")
    config.engine_freeze[engine_name] = freeze_iso


def _clear_engine_freeze(root: Path, config: LitehiveConfig, *, engine_name: str) -> None:
    clear_persisted_engine_freeze(root, engine_name=engine_name, actor="system", source="quota")
    config.engine_freeze.pop(engine_name, None)


def _record_codex_quota_monitoring(root: Path, status: object) -> None:
    try:
        from litehive.observability.engine_monitoring import record_codex_quota_check

        record_codex_quota_check(root, status=status)
    except Exception:  # noqa: BLE001
        pass


def _quota_checker(engine_name: str):
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
    return getattr(status, "short_term", None) is not None and getattr(status, "long_term", None) is not None


def _preferred_quota_reset_at(status: object) -> str | None:
    try:
        return preferred_reset_at(status, include_short_term_fallback=True)
    except AttributeError:
        return None


def _quota_block_reason(engine_name: str, status: object) -> tuple[str | None, str | None]:
    if getattr(status, "error", None) is not None:
        return None, None
    if not _quota_status_uses_unified_shape(status):
        return None, None
    if not bool(getattr(status, "limit_reached", False)):
        return None, None
    reset_at = _preferred_quota_reset_at(status)
    reset_suffix = f", resets {reset_at}" if reset_at else ""
    return f"{engine_name} usage limit reached{reset_suffix}", reset_at


def engine_quota_block(
    root: Path,
    engine_name: str,
) -> tuple[str | None, datetime | None]:
    checker = _quota_checker(engine_name)
    if checker is None:
        return None, None
    status = checker()
    if engine_name == "codex":
        _record_codex_quota_monitoring(root, status)
    reason, reset_at = _quota_block_reason(engine_name, status)
    return reason, _parse_datetime_utc(reset_at)


def select_engine(
    root: Path,
    task: TaskRecord,
    config: LitehiveConfig,
    *,
    engine_override: str | None = None,
    model_override: str | None = None,
    engine_names: list[str] | None = None,
    excluded_engine_names: Collection[str] = (),
    require_available: bool = False,
    check_quota: bool = True,
) -> EngineSelection:
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
            except Exception:  # noqa: BLE001
                pass
        expired_freeze = (
            engine_name not in frozen_engines and _parse_datetime_utc(config.engine_freeze.get(engine_name)) is not None
        )
        quota_reason, freeze_until = engine_quota_block(root, engine_name) if check_quota else (None, None)
        if quota_reason is not None:
            if freeze_until is not None:
                _persist_engine_freeze(root, config, engine_name=engine_name, freeze_until=freeze_until)
            elif expired_freeze:
                _clear_engine_freeze(root, config, engine_name=engine_name)
            skipped.append(EngineSkip(engine_name=engine_name, reason=quota_reason))
            continue
        if expired_freeze:
            _clear_engine_freeze(root, config, engine_name=engine_name)
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
    blocked_reason = skipped[-1].reason if skipped else "no eligible engine available"
    return EngineSelection(
        engine_name=None,
        model_name=None,
        engine_attempts=attempts,
        skipped=skipped,
        blocked_reason=blocked_reason,
    )


def workspace_model_for_engine(config: LitehiveConfig, engine_name: str) -> str | None:
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
    *,
    engine_name: str,
    model_override: str | None = None,
) -> str | None:
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
    *,
    engine_override: str | None = None,
) -> str:
    return resolve_engine_plan(task, config, engine_override=engine_override)[0]


def resolve_engine_attempt_order(
    task: TaskRecord,
    config: LitehiveConfig,
    *,
    engine_override: str | None = None,
) -> list[str]:
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
    *,
    engine_override: str | None = None,
) -> list[str]:
    if engine_override is not None:
        return [engine_override]
    if (
        task.runtime.execution.last_engine_switch is not None
        and task.runtime.execution.last_engine_switch.stage == task.pipeline_status
    ):
        return [task.runtime.execution.last_engine_switch.to_engine]
    return [config.default_engine]


def resolve_task_retry_policy(task: TaskRecord, config: LitehiveConfig) -> int:
    if task.retry_policy.max_retries is not None:
        return task.retry_policy.max_retries
    return config.default_retry_limit


def _resolve_stage_retry_limit(task: TaskRecord, config: LitehiveConfig) -> int:
    if task.retry_policy.stage_retry_limit is not None:
        return task.retry_policy.stage_retry_limit
    return config.default_stage_retry_limit


def resolve_task_rejection_loop_limit(task: TaskRecord, config: LitehiveConfig) -> int:
    if task.retry_policy.rejection_loop_limit is not None:
        return task.retry_policy.rejection_loop_limit
    return config.default_rejection_loop_limit


def _is_recovery_run(task: TaskRecord) -> bool:
    return task.runtime.execution.interruption is not None or task.runtime.pipeline.last_outcome.kind in {
        "flagged",
        "interrupted",
    }


def _role_for_stage(stage: str, task: TaskRecord | None = None) -> str:
    if task is not None and stage in {"implementing", "testing", "accepting"} and _is_recovery_run(task):
        return "recovery"
    return {
        "grooming": "planner",
        "implementing": "swe",
        "testing": "qa",
        "accepting": "reviewer",
    }.get(stage, "swe")
