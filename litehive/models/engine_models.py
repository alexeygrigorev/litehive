"""Engine monitoring and live event models.

The engine-facing types (EngineUsageWindow, EngineUsageObservation,
LiveEvent, LiveTimeline) now live in heru.types. This module re-exports
them and keeps the litehive-only `EngineUsageRecord` /
`WorkspaceEngineMonitoring` authoritative here.
"""

from pydantic import BaseModel, Field

from heru.types import (
    EngineLimitKind,
    EngineMonitoringSource,
    EngineUsageObservation,
    EngineUsageWindow,
    LiveEvent,
    LiveTimeline,
)


class EngineUsageRecord(BaseModel):
    engine: str
    source: EngineMonitoringSource = "local"
    provider: str | None = None
    observed_at: str | None = None
    last_invoked_at: str | None = None
    last_task_id: str | None = None
    last_exit_code: int | None = None
    invocation_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    limit_event_count: int = 0
    last_limit_reason: str | None = None
    last_limit_kind: EngineLimitKind | None = None
    usage: EngineUsageWindow | None = None
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)


class WorkspaceEngineMonitoring(BaseModel):
    engines: dict[str, EngineUsageRecord] = Field(default_factory=dict)


__all__ = [
    "EngineUsageObservation",
    "EngineUsageRecord",
    "EngineUsageWindow",
    "LiveEvent",
    "LiveTimeline",
    "WorkspaceEngineMonitoring",
]
