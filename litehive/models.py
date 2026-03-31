"""Core YAML-backed models for litehive."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


TaskMode = Literal["tasks", "implementation"]
TaskStatus = Literal["queued", "in_progress", "done", "flagged", "cancelled"]
PipelineStatus = Literal[
    "backlog",
    "grooming",
    "implementing",
    "testing",
    "accepting",
    "commit_to_git",
    "done",
]
SubagentStatus = Literal["created", "running", "completed", "failed", "blocked"]


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class SubagentRef(BaseModel):
    id: str
    role: str
    engine: str
    status: SubagentStatus = "created"
    path: str


class RuntimeGitState(BaseModel):
    commit_sha: str | None = None


class RuntimeStageState(BaseModel):
    step: str | None = None
    status: str = "idle"
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str | None = None
    duration_seconds: int = 0
    verdict: str | None = None
    summary: str = ""


class RuntimeSubagentState(BaseModel):
    id: str
    role: str
    engine: str
    status: SubagentStatus
    path: str
    started_at: str
    updated_at: str
    completed_at: str | None = None
    exit_code: int | None = None
    transcript_snippet: str = ""


class RuntimeEngineSwitch(BaseModel):
    step: str
    from_engine: str
    to_engine: str
    reason: str
    happened_at: str = Field(default_factory=utcnow)


class TaskRuntime(BaseModel):
    git: RuntimeGitState = Field(default_factory=RuntimeGitState)
    execution_status: str = "idle"
    run_started_at: str | None = None
    updated_at: str | None = None
    current_stage: RuntimeStageState = Field(default_factory=RuntimeStageState)
    last_stage: RuntimeStageState = Field(default_factory=RuntimeStageState)
    active_subagent: RuntimeSubagentState | None = None
    last_subagent: RuntimeSubagentState | None = None
    last_engine_switch: RuntimeEngineSwitch | None = None


class GitSettings(BaseModel):
    auto_commit: bool = True
    commit_message: str | None = None
    commit_sha: str | None = None
    checkpoint_base_sha: str | None = None
    checkpoint_attempts: int = 0
    rolled_back_checkpoint_attempt: int | None = None


class TaskRecord(BaseModel):
    id: str
    slug: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
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
    runtime: TaskRuntime = Field(default_factory=TaskRuntime, exclude=True)


class WorkspaceState(BaseModel):
    active_task_id: str | None = None
    mode: TaskMode = "implementation"
    queue: list[str] = Field(default_factory=list)


class StageReport(BaseModel):
    task_id: str
    step: Literal["grooming", "implementing", "testing", "accepting", "commit_to_git"]
    verdict: Literal["pass", "accept", "fail", "reject", "blocked"]
    summary: str
    feedback: str = ""
    files_changed: list[str] = Field(default_factory=list)
    tests: dict[str, int] = Field(default_factory=lambda: {"added": 0, "passing": 0})
    warnings: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)
