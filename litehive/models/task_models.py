"""Task and workspace record models."""

from typing import Literal

from pydantic import BaseModel, Field

from .common import (
    HumanCheckpoint,
    PipelineMode,
    PipelineStatus,
    PlannedEffort,
    TaskComplexity,
    TaskMode,
    TaskStatus,
    UpstreamContributionKind,
    utcnow,
)
from .runtime_models import SubagentRef, TaskRuntime


class TaskRetryPolicy(BaseModel):
    max_retries: int | None = None
    stage_retry_limit: int | None = None


class TaskCreationSource(BaseModel):
    task_id: str
    stage: Literal["grooming", "accepting"]
    rationale: str
    blocking: bool = False


class UpstreamPatchProposal(BaseModel):
    branch: str | None = None
    base_ref: str | None = None
    prepared: bool = False
    repo_path: str | None = None


class UpstreamContributionOrigin(BaseModel):
    source_project: str
    source_workspace: str
    source_task_id: str | None = None
    source_task_title: str | None = None
    source_stage: str | None = None
    source_role: str | None = None
    contribution_kind: UpstreamContributionKind
    summary: str = ""
    details: str = ""
    litehive_source_path: str
    patch: UpstreamPatchProposal | None = None


class GitHubOrigin(BaseModel):
    repo: str
    issue_number: int
    issue_url: str
    imported_at: str = Field(default_factory=utcnow)


class GitSettings(BaseModel):
    auto_commit: bool = True
    commit_message: str | None = None
    commit_sha: str | None = None
    checkpoint_base_sha: str | None = None
    checkpoint_attempts: int = 0
    rolled_back_checkpoint_attempt: int | None = None
    merge_agent_attempts: int = 0
    worktree_path: str | None = None


class TaskRecord(BaseModel):
    id: str
    slug: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
    task_type: str | None = None
    engine: str | None = None
    model: str | None = None
    mode: TaskMode = "implementation"
    pipeline_mode: PipelineMode = "full"
    status: TaskStatus = "queued"
    pipeline_status: PipelineStatus = "backlog"
    priority: str = "medium"
    pm_complexity: TaskComplexity | None = None
    planned_effort: PlannedEffort | None = None
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    human_checkpoints: list[HumanCheckpoint] = Field(default_factory=list)
    subagents: list[SubagentRef] = Field(default_factory=list)
    git: GitSettings = Field(default_factory=GitSettings)
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)
    created_from: TaskCreationSource | None = None
    upstream_origin: UpstreamContributionOrigin | None = None
    github_origin: GitHubOrigin | None = None
    runtime: TaskRuntime = Field(default_factory=TaskRuntime, exclude=True)


class UnmergedWorktree(BaseModel):
    task_id: str
    worktree_path: str


class WorkspaceState(BaseModel):
    active_task_id: str | None = None
    mode: TaskMode = "implementation"
    queue: list[str] = Field(default_factory=list)
    pool_stop_reason: str | None = None
    next_task_number: int = 0
    unmerged_worktrees: list[UnmergedWorktree] = Field(default_factory=list)
