"""Workspace-level engine usage and quota monitoring."""

import json
from pathlib import Path
import sqlite3

from litehive.config.paths import workspace_path
from litehive.db.schema import connect_workspace_db
from heru.base import CLIExecutionResult, ExternalCLIAdapter
from heru.quota import preferred_reset_at
from litehive.domain.common import utcnow
from litehive.domain.engine import (
    EngineUsageObservation,
    EngineUsageRecord,
    EngineUsageWindow,
    WorkspaceEngineMonitoring,
)
from litehive.state.locking import workspace_mutation_guard


def load_engine_monitoring(root: Path) -> WorkspaceEngineMonitoring:
    return _load_engine_monitoring_from_db(root)


def save_engine_monitoring(root: Path, monitoring: WorkspaceEngineMonitoring) -> None:
    with workspace_mutation_guard(root):
        _save_engine_monitoring_to_db(root, monitoring)


def _load_engine_monitoring_from_db(root: Path) -> WorkspaceEngineMonitoring:
    db_path = workspace_path(root, "data.db")
    if not db_path.exists():
        return WorkspaceEngineMonitoring()
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'engine_monitoring'",
        ).fetchone()
        if table is None:
            return WorkspaceEngineMonitoring()
        rows = connection.execute(
            "SELECT engine_name, payload FROM engine_monitoring ORDER BY engine_name ASC",
        ).fetchall()
    engines = {}
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            engines[str(row["engine_name"])] = payload
    return WorkspaceEngineMonitoring(engines=engines)


def _save_engine_monitoring_to_db(root: Path, monitoring: WorkspaceEngineMonitoring) -> None:
    with connect_workspace_db(root) as connection:
        connection.execute("DELETE FROM engine_monitoring")
        for engine_name, record in monitoring.engines.items():
            connection.execute(
                """
                INSERT INTO engine_monitoring (engine_name, updated_at, payload)
                VALUES (?, ?, ?)
                """,
                (
                    engine_name,
                    record.observed_at or utcnow(),
                    json.dumps(record.model_dump(mode="json"), sort_keys=True),
                ),
            )
        connection.commit()


def record_engine_execution(
    root: Path,
    *,
    task_id: str,
    engine_name: str,
    adapter: ExternalCLIAdapter,
    execution: CLIExecutionResult,
    failure_kind: str | None,
    failure_reason: str | None,
) -> WorkspaceEngineMonitoring:
    monitoring = load_engine_monitoring(root)
    extract_usage_observation = getattr(adapter, "extract_usage_observation", None)
    observation = (
        extract_usage_observation(execution) if callable(extract_usage_observation) else None
    ) or EngineUsageObservation()
    monitoring = _apply_engine_observation(
        monitoring,
        engine_name=engine_name,
        task_id=task_id,
        execution=execution,
        observation=observation,
        count_invocation=True,
        failure_kind=failure_kind,
        failure_reason=failure_reason,
    )
    save_engine_monitoring(root, monitoring)
    return monitoring


def record_engine_observation(
    root: Path,
    *,
    task_id: str,
    engine_name: str,
    adapter: ExternalCLIAdapter,
    execution: CLIExecutionResult,
) -> WorkspaceEngineMonitoring:
    monitoring = load_engine_monitoring(root)
    extract_usage_observation = getattr(adapter, "extract_usage_observation", None)
    observation = (
        extract_usage_observation(execution) if callable(extract_usage_observation) else None
    ) or EngineUsageObservation()
    if (
        observation.usage is None
        and observation.limit_reason is None
        and not observation.metadata
        and observation.provider is None
    ):
        return monitoring
    monitoring = _apply_engine_observation(
        monitoring,
        engine_name=engine_name,
        task_id=task_id,
        execution=execution,
        observation=observation,
        count_invocation=False,
        failure_kind=None,
        failure_reason=None,
    )
    save_engine_monitoring(root, monitoring)
    return monitoring


def _apply_engine_observation(
    monitoring: WorkspaceEngineMonitoring,
    *,
    engine_name: str,
    task_id: str,
    execution: CLIExecutionResult,
    observation: EngineUsageObservation,
    count_invocation: bool,
    failure_kind: str | None,
    failure_reason: str | None,
) -> WorkspaceEngineMonitoring:
    record = monitoring.engines.get(engine_name)
    if record is None:
        record = EngineUsageRecord(engine=engine_name)

    observed_at = observation.observed_at or utcnow()
    record.source = observation.source
    if observation.provider:
        record.provider = observation.provider
    record.observed_at = observed_at
    record.last_task_id = task_id
    if count_invocation:
        record.invocation_count += max(1, observation.invocation_count)

    if count_invocation:
        success = observation.success
        if success is None:
            success = failure_kind is None and execution.exit_code == 0
        if success:
            record.success_count += 1
        else:
            record.failure_count += 1

    limit_reason = observation.limit_reason or failure_reason
    limit_kind = observation.limit_kind or _limit_kind(limit_reason)
    if count_invocation and limit_reason is not None and limit_kind is not None:
        record.limit_event_count += 1
    if limit_reason is not None and limit_kind is not None:
        record.last_limit_reason = limit_reason
        record.last_limit_kind = limit_kind

    if observation.usage is not None:
        record.usage = observation.usage
    elif count_invocation and record.source == "local":
        record.usage = EngineUsageWindow(
            used=record.invocation_count,
            unit="requests",
        )
    if observation.metadata:
        record.metadata = {**record.metadata, **observation.metadata}

    monitoring.engines[engine_name] = record
    return monitoring


def record_codex_quota_check(
    root: Path,
    *,
    status: object,
) -> None:
    """Record proactive codex quota status into engine monitoring."""
    if getattr(status, "error", None) is not None:
        return  # Don't overwrite good data with error state
    short_term = getattr(status, "short_term", None)
    long_term = getattr(status, "long_term", None)
    if short_term is None or long_term is None:
        return

    monitoring = load_engine_monitoring(root)
    record = monitoring.engines.get("codex")
    if record is None:
        record = EngineUsageRecord(engine="codex")

    record.source = "provider"
    record.provider = "openai"
    record.observed_at = utcnow()

    used_pct = int(_used_percent(long_term))
    reset_at = preferred_reset_at(status)
    record.usage = EngineUsageWindow(
        used=used_pct,
        limit=100,
        remaining=max(0, 100 - used_pct),
        unit="percent",
        reset_at=reset_at,
    )
    limit_reached = bool(getattr(status, "limit_reached", False))
    if limit_reached:
        record.last_limit_reason = "codex usage limit reached"
        record.last_limit_kind = "quota"
    record.metadata = {
        **record.metadata,
        "hours_percent_remaining": int(_percent_remaining(short_term)),
        "weeks_percent_remaining": int(_percent_remaining(long_term)),
        "quota_limit_reached": limit_reached,
    }
    short_reset_at = getattr(short_term, "reset_at", None)
    long_reset_at = getattr(long_term, "reset_at", None)
    if short_reset_at:
        record.metadata["hours_reset_at"] = short_reset_at
    if long_reset_at:
        record.metadata["weeks_reset_at"] = long_reset_at

    monitoring.engines["codex"] = record
    save_engine_monitoring(root, monitoring)


def render_engine_monitoring_lines(monitoring: WorkspaceEngineMonitoring) -> list[str]:
    lines: list[str] = []
    for engine_name in sorted(monitoring.engines):
        record = monitoring.engines[engine_name]
        parts = [
            f"engine_monitoring: {engine_name}",
            f"source={record.source}",
            f"invocations={record.invocation_count}",
            f"success={record.success_count}",
            f"failure={record.failure_count}",
            f"limits={record.limit_event_count}",
        ]
        if record.provider:
            parts.append(f"provider={record.provider}")
        if record.last_limit_kind:
            parts.append(f"last_limit_kind={record.last_limit_kind}")
        if record.last_limit_reason:
            parts.append(f"last_limit_reason={record.last_limit_reason}")
        if record.usage is not None:
            usage_parts: list[str] = []
            if record.usage.used is not None:
                usage_parts.append(f"used={record.usage.used}")
            if record.usage.limit is not None:
                usage_parts.append(f"limit={record.usage.limit}")
            if record.usage.remaining is not None:
                usage_parts.append(f"remaining={record.usage.remaining}")
            if record.usage.unit:
                usage_parts.append(f"unit={record.usage.unit}")
            if record.usage.reset_at:
                usage_parts.append(f"reset_at={record.usage.reset_at}")
            if usage_parts:
                parts.append("usage=" + ",".join(usage_parts))
        if record.last_task_id:
            parts.append(f"last_task={record.last_task_id}")
        if record.observed_at:
            parts.append(f"observed_at={record.observed_at}")
        lines.append(" ".join(parts))
    return lines


def _limit_kind(reason: str | None) -> str | None:
    if not reason:
        return None
    normalized = reason.lower()
    if any(marker in normalized for marker in ("budget", "credit", "insufficient funds")):
        return "budget"
    if any(marker in normalized for marker in ("rate limit", "too many requests")):
        return "rate"
    if "capacity" in normalized:
        return "capacity"
    if any(marker in normalized for marker in ("quota", "usage limit")):
        return "quota"
    return None


def _percent_remaining(window: object) -> float:
    value = getattr(window, "percent_remaining", None)
    if value is None:
        return 100.0
    return float(value)


def _used_percent(window: object) -> float:
    value = getattr(window, "used_percent", None)
    if value is not None:
        return float(value)
    return max(0.0, 100.0 - _percent_remaining(window))
