"""Public shared types for the heru engine adapter layer.

This module is the stable public source for heru's pydantic models.
Callers may rely on these model names, documented fields, and serialized
shapes as part of heru's versioned API contract.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------

SubagentStatus = Literal[
    "created", "running", "completed", "failed", "blocked", "interrupted"
]

EngineMonitoringSource = Literal["provider", "local"]
EngineLimitKind = Literal["quota", "rate", "budget", "capacity"]

LiveEventKind = Literal[
    "message",
    "tool_call",
    "tool_result",
    "error",
    "usage",
    "status",
    "continuation",
]
LiveEventRole = Literal["assistant", "user", "system"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FEEDBACK_CAP = 2000
_TRUNCATION_MARKER = "\n\n… [truncated — full transcript in subagent artifacts]"


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def cap_feedback(text: str, *, limit: int = FEEDBACK_CAP) -> str:
    """Truncate feedback to *limit* characters, appending a marker if trimmed."""
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


# ---------------------------------------------------------------------------
# Engine usage / live events
# ---------------------------------------------------------------------------


class EngineUsageWindow(BaseModel):
    """Public normalized usage-window counters extracted from provider output."""

    used: int | None = None
    limit: int | None = None
    remaining: int | None = None
    unit: str | None = None
    reset_at: str | None = None


class EngineUsageObservation(BaseModel):
    """Public normalized usage or quota observation for one engine run."""

    source: EngineMonitoringSource = "local"
    provider: str | None = None
    observed_at: str = Field(default_factory=utcnow)
    invocation_count: int = 1
    success: bool | None = None
    limit_reason: str | None = None
    limit_kind: EngineLimitKind | None = None
    usage: EngineUsageWindow | None = None
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)


class UnifiedEvent(BaseModel):
    """Public cross-engine JSONL event envelope emitted by heru."""

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
    usage_delta: dict[str, str | int | bool | None] = Field(default_factory=dict)
    continuation_id: str | None = None
    raw: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)


class LiveEvent(UnifiedEvent):
    """Public live-event record stored inside a ``LiveTimeline``."""


class LiveTimeline(BaseModel):
    """Public ordered collection of live events plus summary metadata."""

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


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class ResourceLimitEvent(BaseModel):
    """Public normalized resource-limit failure details."""

    resource: Literal["memory", "cpu", "processes", "resource"] = "resource"
    reason: str
    observed_signal: str | None = None
    exit_code: int | None = None
    memory_mb: int | None = None
    cpu_count: float | None = None
    process_limit: int | None = None


class RuntimeEngineContinuation(BaseModel):
    """Public continuation handle used to resume a prior engine session."""

    session_id: str | None = None
    thread_id: str | None = None
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=utcnow)

    @property
    def resume_id(self) -> str | None:
        """Unified resume identifier — engines call it session_id or thread_id."""
        return self.session_id or self.thread_id


class SubagentRef(BaseModel):
    """Public reference to a subagent reported through pipeline payloads."""

    id: str
    role: str
    engine: str
    status: SubagentStatus = "created"
    path: str
    sandboxed: bool = False
    sandbox_summary: str = ""


__all__ = [
    "EngineLimitKind",
    "EngineMonitoringSource",
    "EngineUsageObservation",
    "EngineUsageWindow",
    "FEEDBACK_CAP",
    "LiveEvent",
    "LiveEventKind",
    "LiveEventRole",
    "LiveTimeline",
    "ResourceLimitEvent",
    "RuntimeEngineContinuation",
    "SubagentRef",
    "SubagentStatus",
    "UnifiedEvent",
    "cap_feedback",
    "utcnow",
]
