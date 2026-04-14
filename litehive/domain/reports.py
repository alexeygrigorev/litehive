"""Stage, recovery, and reporting models (litehive-native)."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .common import (
    FEEDBACK_CAP,
    OutcomeKind,
    OutcomeReasonCode,
    TRUNCATION_MARKER,
    cap_feedback,
    utcnow,
)
from .runtime import ResourceLimitEvent


class StageReport(BaseModel):
    """Persisted stage-report record.

    Historically heru parsed a `STAGE_RESULT: <yaml>` block out of agent
    stdout to build one of these. That path is gone — agents now submit
    verdicts via the `litehive report` CLI (thread comments) and
    `stage_report_from_subagent` constructs this record directly.
    """

    task_id: str
    step: Literal["grooming", "implementing", "testing", "accepting", "commit_to_git"]
    verdict: Literal["pass", "accept", "fail", "reject", "blocked"]
    source: Literal["agent", "hook"] = "agent"
    summary: str
    feedback: str = ""
    submitted_via_cli: bool = False
    files_changed: list[str] = Field(default_factory=list)
    created_follow_up_task_ids: list[str] = Field(default_factory=list)
    tests: dict[str, int] = Field(default_factory=lambda: {"added": 0, "passing": 0})
    warnings: list[str] = Field(default_factory=list)
    retry_count: int = 0
    retry_limit: int = 0
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


class FollowUpTaskSpec(BaseModel):
    title: str
    rationale: str
    blocking: bool = False
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    task_type: str | None = None


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
    "TaskThreadComment",
    "TRUNCATION_MARKER",
    "cap_feedback",
]
