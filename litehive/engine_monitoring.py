"""Workspace-level engine usage and quota monitoring."""

from __future__ import annotations

from pathlib import Path

import yaml

from litehive.config import render_workspace_gitignore, workspace_gitignore_path
from litehive.external_cli import CLIExecutionResult, ExternalCLIAdapter
from litehive.models import (
    EngineUsageObservation,
    EngineUsageRecord,
    EngineUsageWindow,
    WorkspaceEngineMonitoring,
    utcnow,
)
from litehive.tasks import _atomic_write_text, workspace_mutation_guard


def engine_monitoring_file(root: Path) -> Path:
    return root / ".litehive" / "engine-monitoring.yaml"


def load_engine_monitoring(root: Path) -> WorkspaceEngineMonitoring:
    path = engine_monitoring_file(root)
    if not path.exists():
        return WorkspaceEngineMonitoring()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return WorkspaceEngineMonitoring(**data)


def save_engine_monitoring(root: Path, monitoring: WorkspaceEngineMonitoring) -> None:
    with workspace_mutation_guard(root):
        _atomic_write_text(
            engine_monitoring_file(root),
            yaml.safe_dump(monitoring.model_dump(mode="python"), sort_keys=False),
        )
        ignore_path = workspace_gitignore_path(root)
        expected = render_workspace_gitignore()
        if not ignore_path.exists() or ignore_path.read_text(encoding="utf-8") != expected:
            ignore_path.write_text(expected, encoding="utf-8")


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
        extract_usage_observation(execution)
        if callable(extract_usage_observation)
        else None
    ) or EngineUsageObservation()
    record = monitoring.engines.get(engine_name)
    if record is None:
        record = EngineUsageRecord(engine=engine_name)

    observed_at = observation.observed_at or utcnow()
    record.source = observation.source
    if observation.provider:
        record.provider = observation.provider
    record.observed_at = observed_at
    record.last_invoked_at = observed_at
    record.last_task_id = task_id
    record.last_exit_code = execution.exit_code
    record.invocation_count += max(1, observation.invocation_count)

    success = observation.success
    if success is None:
        success = failure_kind is None and execution.exit_code == 0
    if success:
        record.success_count += 1
    else:
        record.failure_count += 1

    limit_reason = observation.limit_reason or failure_reason
    limit_kind = observation.limit_kind or _limit_kind(limit_reason)
    if limit_reason is not None and limit_kind is not None:
        record.limit_event_count += 1
        record.last_limit_reason = limit_reason
        record.last_limit_kind = limit_kind

    if observation.usage is not None:
        record.usage = observation.usage
    elif record.source == "local":
        record.usage = EngineUsageWindow(
            used=record.invocation_count,
            unit="requests",
        )
    if observation.metadata:
        record.metadata = {**record.metadata, **observation.metadata}

    monitoring.engines[engine_name] = record
    save_engine_monitoring(root, monitoring)
    return monitoring


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
