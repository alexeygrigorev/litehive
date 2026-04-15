"""Compatibility helpers for heru-backed shared models.

Litehive's engine integration lives in the sibling ``heru`` package, but a
small subset of Litehive commands only needs the shared record types. When the
engine package is unavailable, keep those task/runtime models importable so
recovery, reporting, and log commands can still run.
"""

from typing import Literal

from pydantic import BaseModel, Field

try:
    from heru.types import (  # type: ignore[import-not-found]
        EngineLimitKind,
        EngineMonitoringSource,
        EngineUsageObservation,
        EngineUsageWindow,
        LiveEvent,
        LiveEventKind,
        LiveEventRole,
        LiveTimeline,
        ResourceLimitEvent,
        RuntimeEngineContinuation,
        SubagentRef,
        SubagentStatus,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised in heru-less environments
    EngineLimitKind = Literal["quota", "rate", "budget", "resource", "unknown"]
    EngineMonitoringSource = Literal["local", "provider"]
    LiveEventKind = Literal[
        "message",
        "status",
        "tool_call",
        "tool_result",
        "error",
        "continuation",
    ]
    LiveEventRole = Literal["assistant", "tool", "system", "user"]
    SubagentStatus = Literal["queued", "running", "completed", "failed", "interrupted", "cancelled"]

    class EngineUsageWindow(BaseModel):
        used: int | float | None = None
        limit: int | float | None = None
        remaining: int | float | None = None
        unit: str | None = None
        reset_at: str | None = None

    class EngineUsageObservation(BaseModel):
        source: EngineMonitoringSource = "local"
        provider: str | None = None
        observed_at: str | None = None
        invocation_count: int = 1
        success: bool | None = None
        limit_reason: str | None = None
        limit_kind: EngineLimitKind | None = None
        usage: EngineUsageWindow | None = None
        metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    class RuntimeEngineContinuation(BaseModel):
        session_id: str | None = None
        cursor: str | None = None
        metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

        @property
        def resume_id(self) -> str | None:
            return self.session_id

    class ResourceLimitEvent(BaseModel):
        resource: str
        reason: str
        observed_signal: str | None = None
        exit_code: int | None = None
        memory_mb: int | None = None
        cpu_count: float | None = None
        process_limit: int | None = None

    class SubagentRef(BaseModel):
        id: str
        role: str
        engine: str
        status: SubagentStatus
        path: str
        sandboxed: bool = False
        sandbox_summary: str = ""

    class LiveEvent(BaseModel):
        kind: LiveEventKind
        role: LiveEventRole | None = None
        content: str | None = None
        error: str | None = None
        tool_name: str | None = None
        tool_input: str | None = None
        tool_output: str | None = None
        continuation_id: str | None = None

    class LiveTimeline(BaseModel):
        engine: str
        task_id: str | None = None
        subagent_id: str | None = None
        events: list[LiveEvent] = Field(default_factory=list)
        message_count: int = 0
        status_count: int = 0
        error_count: int = 0
        tool_call_count: int = 0
        tool_result_count: int = 0

        def recompute_counts(self) -> None:
            self.message_count = sum(1 for event in self.events if event.kind == "message")
            self.status_count = sum(1 for event in self.events if event.kind == "status")
            self.error_count = sum(1 for event in self.events if event.kind == "error")
            self.tool_call_count = sum(1 for event in self.events if event.kind == "tool_call")
            self.tool_result_count = sum(1 for event in self.events if event.kind == "tool_result")


__all__ = [
    "EngineLimitKind",
    "EngineMonitoringSource",
    "EngineUsageObservation",
    "EngineUsageWindow",
    "LiveEvent",
    "LiveEventKind",
    "LiveEventRole",
    "LiveTimeline",
    "ResourceLimitEvent",
    "RuntimeEngineContinuation",
    "SubagentRef",
    "SubagentStatus",
]
