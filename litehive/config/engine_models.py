"""Model and engine resolution and continuation handoff."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import heru.quota.claude_quota as claude_quota_mod
import heru.quota.codex_quota as codex_quota_mod
import heru.quota.copilot_quota as copilot_quota_mod
import heru.quota.zai_quota as zai_quota_mod
import yaml

from heru import extract_engine_continuation, get_engine
from litehive.config.model import LitehiveConfig
from litehive.config.workspace_files import config_path
from litehive.domain.runtime import RuntimeContinuationHandoff
from litehive.domain.task import TaskRecord
from litehive.tasks.runtime import set_task_continuation_handoff


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


def _load_raw_workspace_config(root: Path) -> dict[str, object]:
    path = config_path(root)
    raw_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return dict(raw_data) if isinstance(raw_data, dict) else {}


def _raw_engine_freeze_mapping(raw_data: dict[str, object]) -> dict[str, str]:
    freeze_map = raw_data.get("engine_freeze")
    return dict(freeze_map) if isinstance(freeze_map, dict) else {}


def persist_engine_freeze_iso(root: Path, *, engine_name: str, freeze_iso: str) -> None:
    raw_data = _load_raw_workspace_config(root)
    freeze_map = _raw_engine_freeze_mapping(raw_data)
    if freeze_map.get(engine_name) == freeze_iso:
        return
    freeze_map[engine_name] = freeze_iso
    raw_data["engine_freeze"] = freeze_map
    config_path(root).write_text(yaml.safe_dump(raw_data, sort_keys=False), encoding="utf-8")


def clear_persisted_engine_freeze(root: Path, *, engine_name: str) -> bool:
    raw_data = _load_raw_workspace_config(root)
    freeze_map = _raw_engine_freeze_mapping(raw_data)
    if engine_name not in freeze_map:
        return False
    freeze_map.pop(engine_name)
    if freeze_map:
        raw_data["engine_freeze"] = freeze_map
    else:
        raw_data.pop("engine_freeze", None)
    config_path(root).write_text(yaml.safe_dump(raw_data, sort_keys=False), encoding="utf-8")
    return True


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
    persist_engine_freeze_iso(root, engine_name=engine_name, freeze_iso=freeze_iso)
    config.engine_freeze[engine_name] = freeze_iso


def _clear_engine_freeze(root: Path, config: LitehiveConfig, *, engine_name: str) -> None:
    clear_persisted_engine_freeze(root, engine_name=engine_name)
    config.engine_freeze.pop(engine_name, None)


def _record_codex_quota_monitoring(root: Path, status: object) -> None:
    try:
        from litehive.observability.engine_monitoring import record_codex_quota_check

        record_codex_quota_check(root, status=status)
    except Exception:  # noqa: BLE001
        pass


def _engine_quota_block(
    root: Path,
    engine_name: str,
) -> tuple[str | None, datetime | None]:
    if engine_name == "codex":
        status = codex_quota_mod.check_codex_quota()
        _record_codex_quota_monitoring(root, status)
        if status.error is not None or not status.limit_reached:
            return None, None
        reset_at = _parse_datetime_utc(status.long_term.reset_at)
        reset_info = f", resets {status.long_term.reset_at}" if status.long_term.reset_at else ""
        return (
            f"codex quota exhausted (long-term window at {status.long_term.used_percent:.0f}%{reset_info})",
            reset_at,
        )
    if engine_name == "claude":
        status = claude_quota_mod.check_claude_quota()
        if status.error or not status.limit_reached:
            return None, None
        return (
            f"claude usage limit reached (long-term window at {status.long_term.used_percent:.0f}%, resets {status.long_term.reset_at})",
            _parse_datetime_utc(status.long_term.reset_at),
        )
    if engine_name == "copilot":
        status = copilot_quota_mod.check_copilot_quota()
        if status.error or not status.limit_reached:
            return None, None
        return (
            f"copilot premium requests low ({status.long_term.percent_remaining:.0f}% remaining, resets {status.long_term.reset_at})",
            _parse_datetime_utc(status.long_term.reset_at),
        )
    if engine_name in ("goz", "opencode"):
        status = zai_quota_mod.check_zai_quota()
        if status.error or not status.limit_reached:
            return None, None
        return (
            f"zai usage limit reached (long-term window at {status.long_term.used_percent:.0f}%)",
            _parse_datetime_utc(status.long_term.reset_at),
        )
    return None, None


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
        quota_reason, freeze_until = _engine_quota_block(root, engine_name)
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
    if task.runtime.last_engine_switch is not None and task.runtime.last_engine_switch.stage == task.pipeline_status:
        return [task.runtime.last_engine_switch.to_engine]
    return [config.default_engine]


def resolve_task_retry_policy(task: TaskRecord, config: LitehiveConfig) -> int:
    if task.retry_policy.max_retries is not None:
        return task.retry_policy.max_retries
    return config.default_retry_limit


def _resolve_stage_retry_limit(task: TaskRecord, config: LitehiveConfig) -> int:
    if task.retry_policy.stage_retry_limit is not None:
        return task.retry_policy.stage_retry_limit
    return config.default_stage_retry_limit


def _set_continuation_handoff(
    root: Path,
    task: TaskRecord,
    *,
    stage: str,
    kind: str,
    reason: str,
    result,
    from_engine: str,
    to_engine: str | None,
    from_model: str | None,
    to_model: str | None,
    attempt: int,
) -> RuntimeContinuationHandoff:
    transcript_snippet = ""
    summary = ""
    warnings: list[str] = []
    if result.transcript:
        transcript_snippet = result.transcript.splitlines()[0].strip()
    if result.execution is not None:
        rendered = get_engine(from_engine).render_transcript(result.execution)
        transcript_snippet = transcript_snippet or rendered.splitlines()[0].strip()

    handoff = RuntimeContinuationHandoff(
        stage=stage,
        kind=kind,  # type: ignore[arg-type]
        reason=reason,
        from_engine=from_engine,
        to_engine=to_engine,
        from_model=from_model,
        to_model=to_model,
        subagent_id=result.ref.id,
        subagent_path=result.ref.path,
        status=result.ref.status,
        attempt=attempt,
        summary=summary,
        transcript_snippet=transcript_snippet,
        warnings=warnings,
        session_path=None,
        report_path=None,
        transcript_path=f"{result.ref.path}/transcript.md",
        continuation=extract_engine_continuation(from_engine, result.execution),
    )
    set_task_continuation_handoff(root, task, handoff)
    return handoff


def _is_recovery_run(task: TaskRecord) -> bool:
    if task.runtime.continuation_handoff is not None:
        return True
    return task.runtime.last_outcome.kind in {"flagged", "interrupted"}


def _role_for_stage(stage: str, task: TaskRecord | None = None) -> str:
    if task is not None and stage in {"implementing", "testing", "accepting"} and _is_recovery_run(task):
        return "recovery"
    return {
        "grooming": "planner",
        "implementing": "swe",
        "testing": "qa",
        "accepting": "reviewer",
    }.get(stage, "swe")
