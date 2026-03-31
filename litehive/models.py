"""Core YAML-backed models for litehive."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


TaskMode = Literal["tasks", "implementation"]
TaskStatus = Literal["queued", "in_progress", "done", "flagged", "cancelled"]
PipelineStatus = Literal["backlog", "grooming", "implementing", "testing", "accepting", "done"]
SubagentStatus = Literal["created", "running", "completed", "failed", "blocked"]


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class SubagentRef(BaseModel):
    id: str
    role: str
    engine: str
    status: SubagentStatus = "created"
    path: str


class GitSettings(BaseModel):
    auto_commit: bool = True
    commit_message: str | None = None


class TaskRecord(BaseModel):
    id: str
    slug: str
    title: str
    engine: str | None = None
    mode: TaskMode = "implementation"
    status: TaskStatus = "queued"
    pipeline_status: PipelineStatus = "backlog"
    priority: str = "medium"
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    subagents: list[SubagentRef] = Field(default_factory=list)
    git: GitSettings = Field(default_factory=GitSettings)


class WorkspaceState(BaseModel):
    active_task_id: str | None = None
    mode: TaskMode = "implementation"
    queue: list[str] = Field(default_factory=list)


class StageReport(BaseModel):
    task_id: str
    step: Literal["grooming", "implementing", "testing", "accepting"]
    verdict: Literal["pass", "accept", "fail", "reject", "blocked"]
    summary: str
    feedback: str = ""
    files_changed: list[str] = Field(default_factory=list)
    tests: dict[str, int] = Field(default_factory=lambda: {"added": 0, "passing": 0})
    warnings: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)
