"""
``TaskRecord`` and its persistence-boundary projections.

The aggregate root is ``TaskRecord``: every task-scoped read/write goes
through it. ``TaskIntentRecord`` and ``TaskStateRecord`` project the
record into two halves at the SQLite write boundary so operator-supplied
fields and runtime-managed fields can be saved as separate rows (intent
is rarely written; state churns on every run). ``WorkspaceState`` is
the workspace-wide leftover that doesn't belong on any single task.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import (
    PipelineMode,
    PipelineStatus,
    TaskStatus,
    utcnow,
)
from .runtime import Subagent, TaskRuntime


def canonicalize_task_terminal_state(task: "TaskRecord") -> None:
    """
    Make a task's terminal fields self-consistent before persistence.

    Closed/done tasks must always carry a ``close_reason`` and must
    never simultaneously be flagged; without this normalization the
    operator's status view could show "done + still flagged" or
    "closed without a reason" depending on which write path produced
    the record. Called from every persistence path that ends a task.
    """
    if task.status == TaskStatus.CLOSED:
        outcome_reason_code = task.runtime.pipeline.last_outcome.reason_code
        if outcome_reason_code is None:
            outcome_reason_code_label = None
        else:
            outcome_reason_code_label = outcome_reason_code.value
        task.close_reason = task.close_reason or outcome_reason_code_label or "unknown"
    elif task.status == TaskStatus.DONE:
        task.close_reason = task.close_reason or "done"
    else:
        task.close_reason = None

    if task.status in {TaskStatus.CLOSED, TaskStatus.DONE}:
        task.flag_reason = None


class TaskRetryPolicy(BaseModel):
    """
    Per-task overrides for the retry budget.

    Set at task creation or via task-edit; falls back to workspace
    defaults when ``None``. Read by ``PipelineRunner`` before bumping
    a counter and by recovery logic before deciding whether another
    attempt is allowed. Stage retry vs. overall retry are tracked
    separately so a single chatty stage can't exhaust the whole task
    budget.
    """

    max_retries: int | None = None  # Overall retry limit across all stages
    stage_retry_limit: int | None = None  # Per-stage retry limit
    rejection_loop_limit: int | None = None  # Limit on consecutive rejections


class TaskCreationSource(BaseModel):
    """
    Provenance record stored on every newly-created task.

    Distinguishes manual operator adds, in-agent task creation, and
    follow-up task flows so the operator's task graph can render the
    parent/child relationship and the recovery layer can detect
    runaway agent task creation. ``blocking=True`` partners with
    :func:`blocked_on_follow_up_reason` to wire the parent's
    wait-on-child reason.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["manual", "agent", "follow_up"] = "follow_up"
    task_id: str | None = None  # Parent/current task id when available
    stage: str | None = None  # Parent/current stage when available
    role: str | None = None  # Creating agent role for agent-created tasks
    rationale: str = ""  # Operator or agent explanation for creating this task
    blocking: bool = False  # Whether this task blocks the parent's progress


class GitSettings(BaseModel):
    """
    Combined operator-intent + runtime-state git settings for a task.

    The two halves split at the persistence boundary
    (``TaskIntentGitSettings`` and ``TaskStateGitSettings``) so intent
    rarely writes and state churns per commit, but they live together
    in memory so callers don't have to merge two records every time
    they touch the task's git state.
    """

    auto_commit: bool = True  # Whether to auto-commit changes
    commit_message: str | None = None  # Custom commit message template
    commit_sha: str | None = None  # Current git commit SHA
    checkpoint_attempts: int = 0  # Number of checkpoint attempts
    worktree_path: str | None = None  # Path to git worktree

    def to_intent_git_settings(self) -> "TaskIntentGitSettings":
        """
        Project the operator-controlled fields for the intent row.

        Used by the storage layer when writing the rarely-changing
        intent row; the corresponding runtime fields are split off via
        :meth:`to_state_git_settings`.
        """
        return TaskIntentGitSettings(
            auto_commit=self.auto_commit,
            commit_message=self.commit_message,
        )

    def to_state_git_settings(self) -> "TaskStateGitSettings":
        """
        Project the runtime-tracking fields for the state row.

        Pairs with :meth:`to_intent_git_settings`; together they form
        the two halves stored in the SQLite ``task_intent`` and
        ``task_state`` tables.
        """
        return TaskStateGitSettings(
            commit_sha=self.commit_sha,
            checkpoint_attempts=self.checkpoint_attempts,
            worktree_path=self.worktree_path,
        )


class TaskIntentGitSettings(BaseModel):
    """
    Operator-supplied git settings persisted on the intent row.

    Holds the two operator-controlled knobs (``auto_commit``,
    ``commit_message``) without any runtime tracking, so the intent
    row stays stable across runs and can be rewritten by ``litehive
    update`` without disturbing the per-run state half.
    """

    model_config = ConfigDict(extra="forbid")

    auto_commit: bool = True  # Whether to auto-commit changes
    commit_message: str | None = None  # Custom commit message template


class TaskStateGitSettings(BaseModel):
    """
    Runtime-managed git fields persisted on the state row.

    Holds the per-run tracking (current commit, checkpoint attempts,
    worktree path) without any operator-controlled knobs, so the state
    row can churn on every commit without touching the rarely-written
    intent row.
    """

    commit_sha: str | None = None  # Current git commit SHA
    checkpoint_attempts: int = 0  # Number of checkpoint attempts
    worktree_path: str | None = None  # Path to git worktree

    def to_git_updates(self) -> dict[str, str | int | None]:
        """
        Return the update-dict shape ``GitSettings.model_copy(update=...)`` consumes.

        Used by ``TaskStateRecord.apply_to_task`` when overlaying the
        loaded state row back onto a freshly-built task, so state-side
        fields land on the task without stomping on the operator-intent
        fields that stayed in memory.
        """
        return {
            "commit_sha": self.commit_sha,
            "checkpoint_attempts": self.checkpoint_attempts,
            "worktree_path": self.worktree_path,
        }


class TaskIntentRecord(BaseModel):
    """
    Persistence half of a ``TaskRecord`` carrying operator intent.

    Holds *what* the operator wants done (goal, acceptance criteria,
    constraints, plan, dependencies, mode, priority) plus their git
    preferences. Separated from ``TaskStateRecord`` so editing the
    intent (``litehive update``) doesn't have to traverse the
    state-row schema, and so the rarely-changing intent stays small
    and stable in storage.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    slug: str  # Human-readable task identifier
    title: str  # Brief task summary
    created_at: str = Field(default_factory=utcnow)
    pipeline_mode: PipelineMode = PipelineMode.FULL  # Execution mode (full vs single)
    priority: str = "medium"  # Task scheduling priority
    depends_on: list[str] = Field(default_factory=list)  # Upstream task IDs
    goal: str = ""  # Main intended result
    acceptance_criteria: list[str] = Field(default_factory=list)  # Concrete completion conditions
    constraints: list[str] = Field(default_factory=list)  # Limitations or rules to respect
    plan: list[str] = Field(default_factory=list)  # Current working plan
    git: TaskIntentGitSettings = Field(default_factory=TaskIntentGitSettings)  # Git operator preferences
    created_from: TaskCreationSource | None = None  # What created this task


class TaskStateRecord(BaseModel):
    """
    Persistence half of a ``TaskRecord`` carrying runtime state.

    Holds the per-run mutable fields: status, pipeline position, model
    selection, runtime, retry policy, subagent refs, runtime git
    state. Read by ``PipelineRunner`` and recovery logic; written on
    every transition. Pairs with ``TaskIntentRecord`` —
    :meth:`apply_to_task` reassembles the two halves into a
    ``TaskRecord``.
    """

    model: str | None = None  # AI model being used
    status: TaskStatus = TaskStatus.QUEUED  # High-level execution status
    close_reason: str | None = None  # Reason if task was explicitly closed
    flag_reason: str | None = None  # Reason if task is flagged
    flag_count: int = 0  # Number of times flagged
    pipeline_status: PipelineStatus = PipelineStatus.BACKLOG  # Operator-facing pipeline progress projection
    updated_at: str = Field(default_factory=utcnow)  # Last state change timestamp
    subagents: list[Subagent] = Field(default_factory=list)  # Active/recent subagent records
    git: TaskStateGitSettings = Field(default_factory=TaskStateGitSettings)  # Git execution state
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)  # Retry configuration
    runtime: TaskRuntime = Field(default_factory=TaskRuntime)  # Detailed execution state

    def apply_to_task(self, record: "TaskRecord") -> "TaskRecord":
        """
        Overlay this state snapshot back onto a task built from intent.

        Called by ``TaskRecord.from_intent_and_state`` when
        reassembling a persisted task from its two SQLite rows. The
        intent half is read first to seed identity, then this method
        layers in the runtime-changing fields without disturbing the
        intent half's git operator-knobs.
        """
        record.model = self.model
        record.status = self.status
        record.close_reason = self.close_reason
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
    """
    The aggregate root for a single unit of work tracked by Litehive.

    Carries operator intent (``goal``, ``acceptance_criteria``,
    ``constraints``, ``plan``, ``depends_on``) and execution-attached
    runtime state in one in-memory shape; persistence splits these
    along the ``TaskIntentRecord`` / ``TaskStateRecord`` boundary.
    Read and written by ``PipelineRunner``, recovery, and the CLI;
    subagents and prompts read it for context.

    Mutable fields (``runtime``, lists) use ``Field(default_factory=...)``
    so every new task gets its own instance — a class-level mutable
    default would silently share one ``TaskRuntime`` across multiple
    tasks and corrupt every concurrent run.
    """

    id: str
    slug: str  # Human-readable task identifier
    title: str  # Brief task summary
    depends_on: list[str] = Field(default_factory=list)  # Upstream task IDs that must complete first
    model: str | None = None  # AI model being used for execution
    pipeline_mode: PipelineMode = PipelineMode.FULL  # Execution mode (full vs single stage)
    status: TaskStatus = TaskStatus.QUEUED  # High-level execution or terminal category
    close_reason: str | None = None  # Reason when status is closed or done
    flag_reason: str | None = None  # Reason if task requires operator attention
    flag_count: int = 0  # Number of times task has been flagged
    pipeline_status: PipelineStatus = PipelineStatus.BACKLOG  # Operator-facing pipeline progress projection
    priority: str = "medium"  # Scheduling priority
    created_at: str = Field(default_factory=utcnow)  # Task creation timestamp
    updated_at: str = Field(default_factory=utcnow)  # Last modification timestamp
    goal: str = ""  # Main intended result
    acceptance_criteria: list[str] = Field(default_factory=list)  # Concrete completion conditions
    constraints: list[str] = Field(default_factory=list)  # Limitations or rules that must be respected
    plan: list[str] = Field(default_factory=list)  # Current working plan for the task
    subagents: list[Subagent] = Field(default_factory=list)  # Active/recent subagent records
    git: GitSettings = Field(default_factory=GitSettings)  # Git configuration and state
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)  # Retry limits configuration
    created_from: TaskCreationSource | None = None  # What created this task (if from another task)
    runtime: TaskRuntime = Field(
        default_factory=TaskRuntime, exclude=True
    )  # Mutable execution state, excluded from serialization

    @property
    def current_pipeline_stage(self) -> str | None:
        """
        Return the runtime pipeline stage label for read-only callers.

        The nested ``runtime.pipeline.current_stage`` object remains
        the write/storage shape; most callers only need the current
        stage name and should not know that persistence detail.
        """
        return self.runtime.current_stage_name

    def to_intent_record(self) -> TaskIntentRecord:
        """
        Project the operator-intent half of the task for the intent row.

        Pairs with :meth:`to_state_record`; together they let the
        storage layer write the two SQLite rows without ever observing
        the merged ``TaskRecord`` shape on disk.
        """
        return TaskIntentRecord(
            id=self.id,
            slug=self.slug,
            title=self.title,
            created_at=self.created_at,
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
        """
        Project the runtime-state half of the task for the state row.

        Deep-copies ``retry_policy`` and ``runtime`` so a caller that
        mutates the projection (e.g. for storage normalization) cannot
        accidentally alias the live task's nested objects. Pairs with
        :meth:`to_intent_record`.
        """
        return TaskStateRecord(
            model=self.model,
            status=self.status,
            close_reason=self.close_reason,
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
        """
        State projection prepared for the SQLite write boundary.

        Calls ``TaskRuntime.for_storage`` so the persisted runtime
        carries the latest commit/worktree, and pins
        ``state.git.worktree_path`` to the same value so the round-trip
        through SQLite reads back what was written instead of a value
        that lagged the most recent commit.
        """
        state = self.to_state_record()
        state.runtime = self.runtime.for_storage(
            commit_sha=self.git.commit_sha,
            worktree_path=self.runtime.pipeline.git.worktree_path,
        )
        state.git.worktree_path = self.runtime.pipeline.git.worktree_path
        state.updated_at = self.updated_at
        return state

    @classmethod
    def from_intent_and_state(
        cls,
        intent: TaskIntentRecord,
        state: TaskStateRecord | None = None,
    ) -> "TaskRecord":
        """
        Reassemble a ``TaskRecord`` from its persisted intent + state halves.

        The read-side counterpart of :meth:`to_intent_record` /
        :meth:`to_state_record`. ``state=None`` is supported for
        legacy paths that need an intent-only projection (e.g. before
        the state row has been written for a brand-new task).
        """
        record = cls(**intent.model_dump(mode="python"))
        if state is None:
            return record
        return state.apply_to_task(record)


class UnmergedWorktree(BaseModel):
    """
    Pointer to a worktree whose branch never made it back into main.

    Health and status surfaces render these so abandoned worktrees are
    visible to operators; the worktree-rescue CLI iterates over them
    to offer cherry-pick onto main. Persisted on
    ``WorkspaceState.unmerged_worktrees`` because the entry survives
    the originating task being closed.
    """

    task_id: str
    worktree_path: str


class WorkspaceState(BaseModel):
    """
    Workspace-scoped runtime state that doesn't belong on any single task.

    Holds the active task selection, the task queue, the operator-
    facing pool-stop reason, the consecutive-failure streak that
    drives "halt on repeated failures", a monotonic task numbering
    counter, and the unmerged-worktree list. Lives in one record so
    the daemon and CLI can read/write the workspace's runtime in a
    single round-trip.
    """

    active_task_id: str | None = None
    queue: list[str] = Field(default_factory=list)
    pool_stop_reason: str | None = None
    consecutive_task_failures: int = 0
    next_task_number: int = 0
    unmerged_worktrees: list[UnmergedWorktree] = Field(default_factory=list)
