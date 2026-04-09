"""Stage, recovery, and reporting models."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .common import (
    FEEDBACK_CAP,
    HumanCheckpoint,
    OutcomeKind,
    OutcomeReasonCode,
    PlannedEffort,
    RetrySource,
    TaskComplexity,
    TaskMode,
    TaskStatus,
    _TRUNCATION_MARKER,
    cap_feedback,
    utcnow,
)
from .runtime_models import ResourceLimitEvent


class FollowUpTaskSpec(BaseModel):
    title: str
    rationale: str
    blocking: bool = False
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    task_type: str | None = None


class StageResultTests(BaseModel):
    added: int = 0
    passing: int = 0


class TaskUpdateSubmission(BaseModel):
    """Structured task updates submitted by agents during grooming."""

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
    outcome: TaskStatus | None = None
    outcome_reason: str | None = None
    action: Literal["park", "requeue", "abandon"] | None = None


class StageResultSubmission(BaseModel):
    """Schema-validated structured stage result submitted by agents."""

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


class RecoveryEvidenceItem(BaseModel):
    kind: str
    label: str
    path: str | None = None
    exists: bool = False
    summary: str = ""
    metadata: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)


class RecoveryAction(BaseModel):
    action: str
    applied: bool = True
    summary: str = ""
    metadata: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)


class RecoveryReport(BaseModel):
    task_id: str
    stage: str | None = None
    trigger: str
    summary: str
    failure_classification: str | None = None
    runnable_state: Literal["runnable", "parked", "blocked"] = "blocked"
    blocker: str | None = None
    evidence: list[RecoveryEvidenceItem] = Field(default_factory=list)
    actions: list[RecoveryAction] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    recovery_subagent_id: str | None = None
    recovery_subagent_path: str | None = None
    created_at: str = Field(default_factory=utcnow)


class StageReport(BaseModel):
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
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)
    resource_limit_event: ResourceLimitEvent | None = None
    duration_seconds: int = 0
    hook_results: list[dict[str, str | int | bool | None]] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)


class ExecutionEstimate(BaseModel):
    """Velocity and ETA estimate for task execution."""

    stage_duration_seconds: float = 0.0
    remaining_seconds: float = 0.0
    velocity_stages_per_hour: float = 0.0


class TaskThreadComment(BaseModel):
    """A single comment in the task discussion thread."""

    role: str
    step: str
    verdict: Literal["pass", "reject", "blocked", "comment"] = "comment"
    message: str
    files_changed: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_thread_verdict(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        if normalized in {"accept"}:
            return "pass"
        if normalized in {"fail"}:
            return "reject"
        return normalized


__all__ = [
    "ExecutionEstimate",
    "FEEDBACK_CAP",
    "FollowUpTaskSpec",
    "RecoveryAction",
    "RecoveryEvidenceItem",
    "RecoveryReport",
    "StageReport",
    "StageResultSubmission",
    "StageResultTests",
    "TaskThreadComment",
    "TaskUpdateSubmission",
    "_TRUNCATION_MARKER",
    "cap_feedback",
]
