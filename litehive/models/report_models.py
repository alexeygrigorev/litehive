"""Stage, recovery, and reporting models.

StageReport, StageResultSubmission, StageResultTests, and
TaskUpdateSubmission now live in heru.types. This module re-exports them
and keeps the litehive-only recovery/follow-up/thread models
authoritative here.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from heru.types import (
    StageReport,
    StageResultSubmission,
    StageResultTests,
    TaskUpdateSubmission,
)

from .common import (
    FEEDBACK_CAP,
    TRUNCATION_MARKER,
    cap_feedback,
    utcnow,
)


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
    "StageResultSubmission",
    "StageResultTests",
    "TaskThreadComment",
    "TaskUpdateSubmission",
    "TRUNCATION_MARKER",
    "cap_feedback",
]
