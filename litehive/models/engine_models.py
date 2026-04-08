"""Engine monitoring and live event models."""

from pydantic import BaseModel, Field

from .common import EngineLimitKind, EngineMonitoringSource, LiveEventKind, LiveEventRole, utcnow


class EngineUsageWindow(BaseModel):
    used: int | None = None
    limit: int | None = None
    remaining: int | None = None
    unit: str | None = None
    reset_at: str | None = None


class EngineUsageObservation(BaseModel):
    source: EngineMonitoringSource = "local"
    provider: str | None = None
    observed_at: str = Field(default_factory=utcnow)
    invocation_count: int = 1
    success: bool | None = None
    limit_reason: str | None = None
    limit_kind: EngineLimitKind | None = None
    usage: EngineUsageWindow | None = None
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)


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


class LiveEvent(BaseModel):
    kind: LiveEventKind
    engine: str
    sequence: int = 0
    timestamp: str = Field(default_factory=utcnow)
    role: LiveEventRole | None = None
    content: str = ""
    tool_name: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    error: str | None = None
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)


class LiveTimeline(BaseModel):
    events: list[LiveEvent] = Field(default_factory=list)
    engine: str = ""
    task_id: str | None = None
    subagent_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    event_counts: dict[str, int] = Field(default_factory=dict)

    def recompute_counts(self) -> None:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[event.kind] = counts.get(event.kind, 0) + 1
        self.event_counts = counts
