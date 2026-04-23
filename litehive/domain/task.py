"""Task and workspace record models."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import (
    PipelineMode,
    PipelineStatus,
    TaskStatus,
    utcnow,
)
from .runtime import SubagentRef, TaskRuntime


class TaskRetryPolicy(BaseModel):
    """Configured retry limits for a task.

    Set by task creation and task-edit flows. Used by PipelineRunner and
    recovery logic when deciding whether another attempt is allowed.
    """
    max_retries: int | None = None          # Overall retry limit across all stages
    stage_retry_limit: int | None = None    # Per-stage retry limit
    rejection_loop_limit: int | None = None # Limit on consecutive rejections


class TaskCreationSource(BaseModel):
    """Metadata about what created this task.

    Records the context for task creation across manual/operator task adds,
    in-agent task creation, and follow-up task flows. Helps track task
    relationships and creation provenance for debugging and auditing.
    """
    model_config = ConfigDict(extra="forbid")

    source: Literal["manual", "agent", "follow_up"] = "follow_up"
    task_id: str | None = None    # Parent/current task id when available
    stage: str | None = None      # Parent/current stage when available
    role: str | None = None       # Creating agent role for agent-created tasks
    rationale: str = ""           # Operator or agent explanation for creating this task
    blocking: bool = False        # Whether this task blocks the parent's progress


class GitSettings(BaseModel):
    """Git configuration and state for task execution.

    Combines operator intent (auto_commit, commit_message) with runtime
    state tracking (commit_sha, checkpoint attempts) for git operations
    during task execution.
    """
    auto_commit: bool = True                           # Whether to auto-commit changes
    commit_message: str | None = None                  # Custom commit message template
    commit_sha: str | None = None                      # Current git commit SHA
    checkpoint_base_sha: str | None = None             # Base SHA for checkpointing
    checkpoint_attempts: int = 0                       # Number of checkpoint attempts
    rolled_back_checkpoint_attempt: int | None = None  # Last rolled-back attempt number
    merge_agent_attempts: int = 0                      # Number of merge resolution attempts
    worktree_path: str | None = None                   # Path to git worktree

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
    """Git settings representing operator intent.

    Contains only the operator-specified git configuration, separated from
    runtime state tracking. Used for persistence of operator preferences
    without runtime execution details.
    """
    model_config = ConfigDict(extra="forbid")

    auto_commit: bool = True           # Whether to auto-commit changes
    commit_message: str | None = None  # Custom commit message template


class TaskStateGitSettings(BaseModel):
    """Git state tracking for runtime execution.

    Contains only the runtime state information from git operations,
    separated from operator intent. Used for persistence of execution
    state without operator preferences.
    """
    commit_sha: str | None = None                      # Current git commit SHA
    checkpoint_base_sha: str | None = None             # Base SHA for checkpointing
    checkpoint_attempts: int = 0                       # Number of checkpoint attempts
    rolled_back_checkpoint_attempt: int | None = None  # Last rolled-back attempt number
    merge_agent_attempts: int = 0                      # Number of merge resolution attempts
    worktree_path: str | None = None                   # Path to git worktree

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
    """Operator-specified task intent and configuration.

    Represents the immutable or operator-controlled aspects of a task:
    what the operator wants accomplished, how they want it executed, and
    their planning inputs. Separated from runtime state to maintain a
    clear boundary between operator intent and execution bookkeeping.

    Primary consumers: task creation flows, operator editing, and
    persistence when separating intent from runtime state.
    """
    model_config = ConfigDict(extra="forbid")

    id: str
    slug: str                                                           # Human-readable task identifier
    title: str                                                          # Brief task summary
    created_at: str = Field(default_factory=utcnow)
    task_type: str | None = None                                        # Optional task categorization
    pipeline_mode: PipelineMode = PipelineMode.FULL                     # Execution mode (full vs single)
    priority: str = "medium"                                            # Task scheduling priority
    depends_on: list[str] = Field(default_factory=list)                # Upstream task IDs
    goal: str = ""                                                      # Main intended result
    acceptance_criteria: list[str] = Field(default_factory=list)       # Concrete completion conditions
    constraints: list[str] = Field(default_factory=list)               # Limitations or rules to respect
    plan: list[str] = Field(default_factory=list)                      # Current working plan
    git: TaskIntentGitSettings = Field(default_factory=TaskIntentGitSettings)  # Git operator preferences
    created_from: TaskCreationSource | None = None                     # What created this task


class TaskStateRecord(BaseModel):
    """Runtime execution state and system-managed task properties.

    Represents the mutable execution state that changes as the task
    progresses: current status, pipeline position, execution attempts,
    and runtime tracking. Separated from intent to maintain a clear
    boundary between operator intent and execution bookkeeping.

    Primary consumers: PipelineRunner, RecoveryCoordinator, and persistence
    when separating runtime state from operator intent.
    """
    model: str | None = None                                        # AI model being used
    status: TaskStatus = TaskStatus.QUEUED                          # High-level execution status
    flag_reason: str | None = None                                  # Reason if task is flagged
    flag_count: int = 0                                            # Number of times flagged
    pipeline_status: PipelineStatus = PipelineStatus.BACKLOG       # Current pipeline position
    updated_at: str = Field(default_factory=utcnow)               # Last state change timestamp
    subagents: list[SubagentRef] = Field(default_factory=list)    # Active/recent subagent references
    git: TaskStateGitSettings = Field(default_factory=TaskStateGitSettings)  # Git execution state
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)   # Retry configuration
    runtime: TaskRuntime = Field(default_factory=TaskRuntime)                # Detailed execution state

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
    """A unit of work tracked by Litehive - the main aggregate root.

    Created by TaskService when the operator creates a new task, or by
    follow-up task creation flows when one task produces another task.
    Exists to store the operator's intended work item plus its execution-
    attached runtime state.

    Used by PipelineRunner, RecoveryCoordinator, and CLI commands to read
    and update task progress. Also used by subagents and prompts to
    understand the task goal, plan, and constraints.

    This is the main aggregate boundary in the system - prefer loading and
    changing task-scoped data through task-oriented services instead of
    letting unrelated modules mutate pieces independently.

    Field meanings:
    - goal: the main intended result
    - acceptance_criteria: concrete completion conditions
    - constraints: limitations or rules that must be respected
    - plan: the current working plan for the task
    - depends_on: upstream task ids
    - runtime: mutable execution-only state, separated from task intent

    Why default_factory is used for runtime and other mutable fields:
    TaskRuntime is mutable state, so each Task must get its own fresh
    TaskRuntime instance. Field(default_factory=TaskRuntime) creates a
    new instance per task. Using runtime: TaskRuntime = TaskRuntime()
    directly would risk sharing one instance across multiple tasks.
    """
    id: str
    slug: str                                                           # Human-readable task identifier
    title: str                                                          # Brief task summary
    depends_on: list[str] = Field(default_factory=list)                # Upstream task IDs that must complete first
    task_type: str | None = None                                        # Optional task categorization
    model: str | None = None                                            # AI model being used for execution
    pipeline_mode: PipelineMode = PipelineMode.FULL                     # Execution mode (full vs single stage)
    status: TaskStatus = TaskStatus.QUEUED                              # High-level execution or terminal category
    flag_reason: str | None = None                                      # Reason if task requires operator attention
    flag_count: int = 0                                                # Number of times task has been flagged
    pipeline_status: PipelineStatus = PipelineStatus.BACKLOG           # Current position in pipeline
    priority: str = "medium"                                            # Scheduling priority
    created_at: str = Field(default_factory=utcnow)                    # Task creation timestamp
    updated_at: str = Field(default_factory=utcnow)                    # Last modification timestamp
    goal: str = ""                                                      # Main intended result
    acceptance_criteria: list[str] = Field(default_factory=list)       # Concrete completion conditions
    constraints: list[str] = Field(default_factory=list)               # Limitations or rules that must be respected
    plan: list[str] = Field(default_factory=list)                      # Current working plan for the task
    subagents: list[SubagentRef] = Field(default_factory=list)        # Active/recent subagent references
    git: GitSettings = Field(default_factory=GitSettings)              # Git configuration and state
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)  # Retry limits configuration
    created_from: TaskCreationSource | None = None                     # What created this task (if from another task)
    runtime: TaskRuntime = Field(default_factory=TaskRuntime, exclude=True)  # Mutable execution state, excluded from serialization

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
