"""
Engine monitoring records persisted on workspace state.

The engine-facing primitives (``EngineUsageWindow``, ``EngineUsageObservation``,
``LiveEvent``, ``LiveEventStream``) live in ``heru.types`` so the engine
client and the workspace see the same shapes. This module owns the two
litehive-side aggregations: ``EngineUsageRecord`` (per-engine counters)
and ``WorkspaceEngineMonitoring`` (workspace-wide map). Both are read by
the status output and the engine-routing decisions; updates flow through
the runner as it observes engine outcomes.
"""

from pydantic import BaseModel, Field

from heru.types import (
    EngineLimitKind,
    EngineMonitoringSource,
    EngineUsageObservation,
    EngineUsageWindow,
    LiveEvent,
    LiveTimeline as LiveEventStream,
)


class EngineUsageRecord(BaseModel):
    """
    Per-engine counters and the most recent usage window.

    Aggregates invocation counts, success/failure splits, and resource-limit
    events so engine-routing logic can prefer healthy engines and operator
    surfaces can show why an engine was avoided. Updated by the runner each
    time it observes an engine outcome; persisted as part of
    ``WorkspaceEngineMonitoring``.
    """

    engine: str  # Engine name
    source: EngineMonitoringSource = "local"  # Where this data came from
    provider: str | None = None  # AI provider if applicable
    observed_at: str | None = None  # When this record was captured
    last_task_id: str | None = None  # Task ID of most recent invocation
    invocation_count: int = 0  # Total invocations
    success_count: int = 0  # Successful invocations
    failure_count: int = 0  # Failed invocations
    limit_event_count: int = 0  # Resource limit events
    last_limit_reason: str | None = None  # Most recent limit reason
    last_limit_kind: EngineLimitKind | None = None  # Most recent limit type
    usage: EngineUsageWindow | None = None  # Detailed usage window
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)  # Additional context


class WorkspaceEngineMonitoring(BaseModel):
    """
    Workspace-wide map from engine name to ``EngineUsageRecord``.

    Single source of truth for engine health across a workspace; the
    routing helper consults this to skip engines that have hit limits
    and the status output reads it to render the engine-monitoring
    block. Created on demand if missing; never split per-task.
    """

    engines: dict[str, EngineUsageRecord] = Field(default_factory=dict)  # Engine name to usage record mapping


__all__ = [
    "EngineUsageObservation",
    "EngineUsageRecord",
    "EngineUsageWindow",
    "LiveEvent",
    "LiveEventStream",
    "WorkspaceEngineMonitoring",
]
