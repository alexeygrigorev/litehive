"""Task and workspace record models."""

from pydantic import BaseModel, ConfigDict, Field

from .common import (
    PipelineMode,
    PipelineStatus,
    TaskStage,
    TaskStatus,
    utcnow,
)
from .runtime import SubagentRef, TaskRuntime


class TaskRetryPolicy(BaseModel):
    max_retries: int | None = None
    stage_retry_limit: int | None = None
    rejection_loop_limit: int | None = None


class TaskCreationSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    stage: TaskStage
    rationale: str
    blocking: bool = False


class GitSettings(BaseModel):
    auto_commit: bool = True
    commit_message: str | None = None
    commit_sha: str | None = None
    checkpoint_base_sha: str | None = None
    checkpoint_attempts: int = 0
    rolled_back_checkpoint_attempt: int | None = None
    merge_agent_attempts: int = 0
    worktree_path: str | None = None

    def to_intent_git_settings(self) -> "TaskIntentGitSettings":
        return TaskIntentGitSettings(
            auto_commit=self.auto_commit,
            commit_message=self.commit_message,
        )

    def to_state_git_settings(self) -> "TaskStateGitSettings":
        return TaskStateGitSettings(
            commit_sha=self.commit_sha,
            checkpoint_base_sha=self.checkpoint_base_sha,
            checkpoint_attempts=self.checkpoint_attempts,
            rolled_back_checkpoint_attempt=self.rolled_back_checkpoint_attempt,
            merge_agent_attempts=self.merge_agent_attempts,
            worktree_path=self.worktree_path,
        )


class TaskIntentGitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_commit: bool = True
    commit_message: str | None = None


class TaskStateGitSettings(BaseModel):
    commit_sha: str | None = None
    checkpoint_base_sha: str | None = None
    checkpoint_attempts: int = 0
    rolled_back_checkpoint_attempt: int | None = None
    merge_agent_attempts: int = 0
    worktree_path: str | None = None

    def to_git_updates(self) -> dict[str, str | int | None]:
        return {
            "commit_sha": self.commit_sha,
            "checkpoint_base_sha": self.checkpoint_base_sha,
            "checkpoint_attempts": self.checkpoint_attempts,
            "rolled_back_checkpoint_attempt": self.rolled_back_checkpoint_attempt,
            "merge_agent_attempts": self.merge_agent_attempts,
            "worktree_path": self.worktree_path,
        }


class TaskIntentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    slug: str
    title: str
    created_at: str = Field(default_factory=utcnow)
    task_type: str | None = None
    pipeline_mode: PipelineMode = PipelineMode.FULL
    priority: str = "medium"
    depends_on: list[str] = Field(default_factory=list)
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    git: TaskIntentGitSettings = Field(default_factory=TaskIntentGitSettings)
    created_from: TaskCreationSource | None = None


class TaskStateRecord(BaseModel):
    model: str | None = None
    status: TaskStatus = TaskStatus.QUEUED
    flag_reason: str | None = None
    flag_count: int = 0
    pipeline_status: PipelineStatus = PipelineStatus.BACKLOG
    updated_at: str = Field(default_factory=utcnow)
    subagents: list[SubagentRef] = Field(default_factory=list)
    git: TaskStateGitSettings = Field(default_factory=TaskStateGitSettings)
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)
    runtime: TaskRuntime = Field(default_factory=TaskRuntime)

    def apply_to_task(self, record: "TaskRecord") -> "TaskRecord":
        record.model = self.model
        record.status = self.status
        record.flag_reason = self.flag_reason
        record.flag_count = self.flag_count
        record.pipeline_status = self.pipeline_status
        record.updated_at = self.updated_at
        record.subagents = list(self.subagents)
        record.git = record.git.model_copy(update=self.git.to_git_updates())
        record.retry_policy = self.retry_policy.model_copy(deep=True)
        record.runtime = self.runtime.model_copy(deep=True)
        return record


class TaskRecord(BaseModel):
    id: str
    slug: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
    task_type: str | None = None
    model: str | None = None
    pipeline_mode: PipelineMode = PipelineMode.FULL
    status: TaskStatus = TaskStatus.QUEUED
    flag_reason: str | None = None
    flag_count: int = 0
    pipeline_status: PipelineStatus = PipelineStatus.BACKLOG
    priority: str = "medium"
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    subagents: list[SubagentRef] = Field(default_factory=list)
    git: GitSettings = Field(default_factory=GitSettings)
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)
    created_from: TaskCreationSource | None = None
    runtime: TaskRuntime = Field(default_factory=TaskRuntime, exclude=True)

    def to_intent_record(self) -> TaskIntentRecord:
        return TaskIntentRecord(
            id=self.id,
            slug=self.slug,
            title=self.title,
            created_at=self.created_at,
            task_type=self.task_type,
            pipeline_mode=self.pipeline_mode,
            priority=self.priority,
            depends_on=list(self.depends_on),
            goal=self.goal,
            acceptance_criteria=list(self.acceptance_criteria),
            constraints=list(self.constraints),
            plan=list(self.plan),
            git=self.git.to_intent_git_settings(),
            created_from=self.created_from,
        )

    def to_state_record(self) -> TaskStateRecord:
        return TaskStateRecord(
            model=self.model,
            status=self.status,
            flag_reason=self.flag_reason,
            flag_count=self.flag_count,
            pipeline_status=self.pipeline_status,
            updated_at=self.updated_at,
            subagents=list(self.subagents),
            git=self.git.to_state_git_settings(),
            retry_policy=self.retry_policy.model_copy(deep=True),
            runtime=self.runtime.model_copy(deep=True),
        )

    def to_storage_state_record(self) -> TaskStateRecord:
        state = self.to_state_record()
        state.runtime = self.runtime.for_storage(
            commit_sha=self.git.commit_sha,
            worktree_path=self.runtime.git.worktree_path,
        )
        state.git.worktree_path = self.runtime.git.worktree_path
        state.updated_at = self.updated_at
        return state

    @classmethod
    def from_intent_and_state(
        cls,
        intent: TaskIntentRecord,
        state: TaskStateRecord | None = None,
    ) -> "TaskRecord":
        record = cls(**intent.model_dump(mode="python"))
        if state is None:
            return record
        return state.apply_to_task(record)


class UnmergedWorktree(BaseModel):
    task_id: str
    worktree_path: str


class WorkspaceState(BaseModel):
    active_task_id: str | None = None
    queue: list[str] = Field(default_factory=list)
    pool_stop_reason: str | None = None
    next_task_number: int = 0
    unmerged_worktrees: list[UnmergedWorktree] = Field(default_factory=list)
