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
    """Usage and monitoring data for a specific engine.

    Tracks engine invocation patterns, success/failure rates, resource
    limits, and usage windows for monitoring and routing decisions.
    """
    engine: str                                         # Engine name
    source: EngineMonitoringSource = "local"           # Where this data came from
    provider: str | None = None                         # AI provider if applicable
    observed_at: str | None = None                      # When this record was captured
    last_task_id: str | None = None                     # Task ID of most recent invocation
    invocation_count: int = 0                          # Total invocations
    success_count: int = 0                             # Successful invocations
    failure_count: int = 0                             # Failed invocations
    limit_event_count: int = 0                         # Resource limit events
    last_limit_reason: str | None = None               # Most recent limit reason
    last_limit_kind: EngineLimitKind | None = None     # Most recent limit type
    usage: EngineUsageWindow | None = None             # Detailed usage window
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)  # Additional context


class WorkspaceEngineMonitoring(BaseModel):
    """Aggregated engine monitoring data for a workspace.

    Contains usage records for all engines used within a workspace,
    providing a complete view of engine utilization patterns.
    """
    engines: dict[str, EngineUsageRecord] = Field(default_factory=dict)  # Engine name to usage record mapping


__all__ = [
    "EngineUsageObservation",
    "EngineUsageRecord",
    "EngineUsageWindow",
    "LiveEvent",
    "LiveTimeline",
    "WorkspaceEngineMonitoring",
]
