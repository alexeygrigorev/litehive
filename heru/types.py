"""Public shared types for the heru engine adapter layer.

This module is the stable public source for heru's pydantic models.
Callers may rely on these model names, documented fields, and serialized
shapes as part of heru's versioned API contract.

NOTE: StageReport, StageResultSubmission, StageResultTests, and
TaskUpdateSubmission live here temporarily. They are really a litehive
pipeline concept (the ``STAGE_RESULT:`` agent protocol and the retry
bookkeeping fields belong to the orchestrator). Moving them out of heru
is tracked as a follow-up on the litehive side.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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

RetrySource = Literal["global", "task"]

OutcomeKind = Literal[
    "flagged",
    "blocked",
    "interrupted",
    "cancelled",
    "wont_do",
    "deferred",
    "duplicate",
]

OutcomeReasonCode = Literal[
    "verdict_fail",
    "verdict_reject",
    "verdict_blocked",
    "hallucinated_completion",
    "resource_limit",
    "missing_acceptance_criteria",
    "retry_limit_exhausted",
    "stage_retry_limit_exhausted",
    "execution_interrupted",
    "execution_cancelled",
    "stage_exception",
    "unsupported_verdict",
    "merge_conflict",
    "wont_do",
    "deferred",
    "duplicate",
]

TaskComplexity = Literal["simple", "moderate", "complex"]
PlannedEffort = Literal["xs", "s", "m", "l", "xl"]
HumanCheckpoint = Literal["before_acceptance", "before_commit"]
TaskMode = Literal["tasks", "implementation"]


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


# ---------------------------------------------------------------------------
# Stage reports
#
# These really belong to the litehive pipeline layer (the ``STAGE_RESULT:``
# agent protocol, retry bookkeeping, outcome classification) and will move
# out of heru in a follow-up. They live here for now so heru is importable
# standalone.
# ---------------------------------------------------------------------------


class StageResultTests(BaseModel):
    """Public structured counts for tests added and passing in a stage result."""

    added: int = 0
    passing: int = 0


class TaskUpdateSubmission(BaseModel):
    """Structured task updates submitted by agents during grooming.

    This model is currently public because litehive consumes it through
    heru today, even though it is expected to move out of heru later.

    ``outcome``/``priority``/``task_type`` are typed as ``str | None`` because
    heru only passes these fields through to litehive; the concrete allowed
    values (TaskStatus etc.) are validated by the orchestrator on the
    litehive side, not by the engine adapter layer.
    """

    title: str | None = None
    goal: str | None = None
    acceptance_criteria: list[str] | None = None
    constraints: list[str] | None = None
    plan: list[str] | None = None
    pm_complexity: TaskComplexity | None = None
    planned_effort: PlannedEffort | None = None
    depends_on: list[str] | None = None
    human_checkpoints: list[HumanCheckpoint] | None = None
    task_type: str | None = None
    mode: TaskMode | None = None
    priority: str | None = None
    engine: str | None = None
    model: str | None = None
    retry_limit: int | None = None
    auto_commit: bool | None = None
    outcome: str | None = None
    outcome_reason: str | None = None
    action: Literal["park", "requeue", "abandon"] | None = None


class StageResultSubmission(BaseModel):
    """Public schema for structured stage result submissions."""

    model_config = {"extra": "forbid"}

    verdict: Literal["pass", "reject"]
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    tests: StageResultTests = Field(default_factory=StageResultTests)
    warnings: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    task_update: TaskUpdateSubmission | None = None

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_agent_submission_verdict(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        if normalized in {"accept"}:
            return "pass"
        if normalized in {"fail", "blocked"}:
            return "reject"
        return normalized


class StageReport(BaseModel):
    """Public persisted stage-report record exchanged with litehive today."""

    task_id: str
    step: Literal["grooming", "implementing", "testing", "accepting", "commit_to_git"]
    verdict: Literal["pass", "accept", "fail", "reject", "blocked"]
    source: Literal["agent", "hook"] = "agent"
    summary: str
    feedback: str = ""
    submitted_via_cli: bool = False
    files_changed: list[str] = Field(default_factory=list)
    created_follow_up_task_ids: list[str] = Field(default_factory=list)
    task_update: dict[str, object] = Field(default_factory=dict)
    tests: dict[str, int] = Field(default_factory=lambda: {"added": 0, "passing": 0})
    warnings: list[str] = Field(default_factory=list)
    retry_count: int = 0
    retry_limit: int = 0
    retry_source: RetrySource = "global"
    retry_decision: Literal["continue", "retry", "final"] = "continue"
    outcome: OutcomeKind | None = None
    outcome_reason_code: OutcomeReasonCode | None = None
    outcome_reason: str = ""
    failure_classification: str | None = None
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] = Field(
        default_factory=dict
    )
    resource_limit_event: ResourceLimitEvent | None = None
    duration_seconds: int = 0
    hook_results: list[dict[str, str | int | bool | None]] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)


__all__ = [
    "EngineLimitKind",
    "EngineMonitoringSource",
    "EngineUsageObservation",
    "EngineUsageWindow",
    "FEEDBACK_CAP",
    "HumanCheckpoint",
    "LiveEvent",
    "LiveEventKind",
    "LiveEventRole",
    "LiveTimeline",
    "OutcomeKind",
    "OutcomeReasonCode",
    "PlannedEffort",
    "ResourceLimitEvent",
    "RetrySource",
    "RuntimeEngineContinuation",
    "StageReport",
    "StageResultSubmission",
    "StageResultTests",
    "SubagentRef",
    "SubagentStatus",
    "TaskComplexity",
    "TaskMode",
    "TaskUpdateSubmission",
    "UnifiedEvent",
    "cap_feedback",
    "utcnow",
]
