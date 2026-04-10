"""Model and engine resolution, retry policy, and continuation handoff."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from litehive.config import ExecutionRetryPolicy, LitehiveConfig
from litehive.agents import extract_engine_continuation, get_engine
import litehive.agents.quota.claude_quota as claude_quota_mod
import litehive.agents.quota.codex_quota as codex_quota_mod
import litehive.agents.quota.copilot_quota as copilot_quota_mod
import litehive.agents.quota.zai_quota as zai_quota_mod
from litehive.models import RuntimeContinuationHandoff, TaskRecord
from litehive.workspace.runtime_tracking import set_task_continuation_handoff
from litehive.config.paths import config_path

from ._budget import _engine_attempt_order
from ._types import ResolvedExecutionRetryPolicy


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
    path = config_path(root)
    raw_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw_data, dict):
        raw_data = {}
    freeze_map = raw_data.get("engine_freeze", {})
    if not isinstance(freeze_map, dict):
        freeze_map = {}
    freeze_map[engine_name] = freeze_iso
    raw_data["engine_freeze"] = freeze_map
    path.write_text(yaml.safe_dump(raw_data, sort_keys=False), encoding="utf-8")
    config.engine_freeze[engine_name] = freeze_iso


def _record_codex_quota_monitoring(root: Path, status: object) -> None:
    try:
        from litehive.observability._engine_monitoring import record_codex_quota_check

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
        reset_at = _parse_datetime_utc(status.earliest_reset_at)
        reset_info = f", resets at {status.earliest_reset_at}" if status.earliest_reset_at else ""
        return f"codex quota exhausted (used {status.max_used_percent:.0f}%{reset_info})", reset_at
    if engine_name == "claude":
        status = claude_quota_mod.check_claude_quota()
        if status.error or not status.limit_reached:
            return None, None
        window = "5h" if status.five_hour.used_percent >= 80 else "7d"
        pct = status.five_hour.used_percent if window == "5h" else status.seven_day.used_percent
        reset = status.five_hour.reset_at if window == "5h" else status.seven_day.reset_at
        return f"claude usage limit reached ({window} window at {pct:.0f}%, resets {reset})", _parse_datetime_utc(reset)
    if engine_name == "copilot":
        status = copilot_quota_mod.check_copilot_quota()
        if status.error or not status.limit_reached:
            return None, None
        return (
            f"copilot premium requests low ({status.premium_remaining}/{status.premium_entitlement} "
            f"remaining, {status.premium_percent_remaining:.0f}%, resets {status.quota_reset_date})",
            _parse_datetime_utc(status.quota_reset_date),
        )
    if engine_name in ("goz", "opencode"):
        status = zai_quota_mod.check_zai_quota()
        if status.error or not status.limit_reached:
            return None, None
        return f"zai usage limit reached ({status.max_used_percent:.0f}% used)", None
    return None, None


def select_engine(
    root: Path,
    task: TaskRecord,
    config: LitehiveConfig,
    *,
    budget_ledger=None,
    engine_override: str | None = None,
    model_override: str | None = None,
    engine_names: list[str] | None = None,
    require_available: bool = False,
) -> EngineSelection:
    if engine_names is not None:
        order = _dedupe_engine_names(engine_names)
    else:
        plan = resolve_engine_plan(
            task,
            config,
            engine_override=engine_override,
        )
        order = _engine_attempt_order(plan, config.engine_preference)
    frozen_engines = active_engine_freezes(config)
    attempts = [engine_name for engine_name in order if engine_name not in frozen_engines]
    skipped: list[EngineSkip] = []
    if not attempts and frozen_engines:
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
        quota_reason, freeze_until = _engine_quota_block(root, engine_name)
        if quota_reason is not None:
            if freeze_until is not None:
                _persist_engine_freeze(root, config, engine_name=engine_name, freeze_until=freeze_until)
            skipped.append(EngineSkip(engine_name=engine_name, reason=quota_reason))
            continue
        if budget_ledger is not None:
            budget_reason = budget_ledger.block_reason(engine_name)
            if budget_reason is not None:
                skipped.append(EngineSkip(engine_name=engine_name, reason=budget_reason))
                continue
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
    return [config.default_engine]


def resolve_task_retry_policy(task: TaskRecord, config: LitehiveConfig) -> tuple[int, str]:
    if task.retry_policy.max_retries is not None:
        return task.retry_policy.max_retries, "task"
    return config.default_retry_limit, "global"


def _resolve_stage_retry_limit(task: TaskRecord, config: LitehiveConfig) -> int:
    if task.retry_policy.stage_retry_limit is not None:
        return task.retry_policy.stage_retry_limit
    return config.default_stage_retry_limit


def _execution_retry_model_family(*, engine_name: str, model_name: str | None) -> str:
    if model_name:
        model_tail = model_name.rsplit("/", 1)[-1].strip().lower()
        match = re.match(r"[a-z0-9]+", model_tail)
        if match is not None:
            return match.group(0)
    return engine_name


def resolve_execution_retry_policy(
    config: LitehiveConfig, *, engine_name: str, model_name: str | None = None
) -> ResolvedExecutionRetryPolicy:
    model_family = _execution_retry_model_family(engine_name=engine_name, model_name=model_name)
    selector_order = [engine_name, f"model_family:{model_family}", "external_cli"]
    for selector in selector_order:
        if selector in config.execution_retry_policies:
            return ResolvedExecutionRetryPolicy(
                selector=selector,
                policy=config.execution_retry_policies[selector],
            )
    return ResolvedExecutionRetryPolicy(selector="none", policy=ExecutionRetryPolicy())


def _retry_backoff_seconds(policy: ExecutionRetryPolicy, retry_number: int) -> float:
    if retry_number <= 0:
        return 0.0
    return policy.backoff_seconds * (policy.backoff_multiplier ** (retry_number - 1))


def _set_continuation_handoff(
    root: Path,
    task: TaskRecord,
    *,
    step: str,
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
        if rendered.strip():
            report = get_engine(from_engine).parse_stage_report(
                task_id=task.id,
                step=step,  # type: ignore[arg-type]
                execution=result.execution,
                subagent_status=result.ref.status,
            )
            summary = report.summary
            warnings = list(report.warnings)

    handoff = RuntimeContinuationHandoff(
        step=step,
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
        session_path=f"{result.ref.path}/session.yaml",
        report_path=f"{result.ref.path}/report.yaml",
        transcript_path=f"{result.ref.path}/transcript.md",
        continuation=extract_engine_continuation(from_engine, result.execution),
    )
    set_task_continuation_handoff(root, task, handoff)
    return handoff


def _is_recovery_run(task: TaskRecord) -> bool:
    if task.runtime.continuation_handoff is not None:
        return True
    return task.runtime.last_outcome.kind in {"flagged", "interrupted"}


def _role_for_step(step: str, task: TaskRecord | None = None) -> str:
    if (
        task is not None
        and step in {"implementing", "testing", "accepting"}
        and _is_recovery_run(task)
    ):
        return "recovery"
    return {
        "grooming": "planner",
        "implementing": "swe",
        "testing": "qa",
        "accepting": "reviewer",
    }.get(step, "swe")
