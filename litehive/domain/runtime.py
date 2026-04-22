"""Runtime and execution state models.

ResourceLimitEvent and RuntimeEngineContinuation now live in heru.types.
This module re-exports them and keeps the litehive-only runtime state
models (RuntimeGitState, RuntimeStageState, etc.) authoritative here.
"""

from typing import Literal

from pydantic import BaseModel, Field

from heru.types import (
    ResourceLimitEvent,
    RuntimeEngineContinuation,
    SubagentRef as HeruSubagentRef,
)

from .common import (
    OutcomeKind,
    OutcomeReasonCode,
    RunnerExecutionStatus,
    SubagentStatus,
    utcnow,
)


class RuntimeGitState(BaseModel):
    """Git state tracking for runtime execution.

    Tracks the current git context where the task is executing.
    Updated by PipelineRunner as git operations occur.
    """
    commit_sha: str | None = None      # Current git commit SHA being worked on
    worktree_path: str | None = None   # Path to the git worktree if using worktrees


class RuntimeStageState(BaseModel):
    """Runtime state for a specific pipeline stage execution.

    Tracks the current or most recent stage execution details.
    Used by PipelineRunner to record stage progress and by CLI/reporting
    for displaying current stage status.
    """
    stage: str | None = None          # Pipeline stage being executed
    status: str = "idle"              # Execution status (idle, running, completed, failed)
    started_at: str | None = None     # When stage execution started
    completed_at: str | None = None   # When stage execution finished
    updated_at: str | None = None     # Last status update timestamp
    duration_seconds: int = 0         # How long the stage has been running
    verdict: str | None = None        # Final verdict (accept, reject, blocked) if completed
    summary: str = ""                 # Brief description of stage results

    @property
    def step(self) -> str | None:
        return self.stage

    @step.setter
    def step(self, value: str | None) -> None:
        self.stage = value


class RuntimeSubagentState(BaseModel):
    """Runtime state for a subagent execution.

    Tracks the lifecycle and current status of an individual subagent.
    Created when a subagent starts, updated during execution, and finalized
    when the subagent completes or is interrupted.

    Used by PipelineRunner for subagent lifecycle management and by
    CLI/reporting for displaying active subagent status. Note that detailed
    logs and traces belong in artifacts, not in this runtime state.
    """
    id: str                                                # Unique subagent identifier
    role: str                                              # Execution role (planner, swe, qa, etc.)
    engine: str                                            # AI engine being used
    status: SubagentStatus                                 # Current lifecycle status
    path: str                                              # Filesystem path where subagent runs
    pid: int | None = None                                 # Process ID if applicable
    sandboxed: bool = False                                # Whether running in sandbox
    sandbox_summary: str = ""                              # Brief sandbox status
    started_at: str                                        # Subagent start timestamp
    updated_at: str                                        # Last status update timestamp
    completed_at: str | None = None                        # Completion timestamp if finished
    exit_code: int | None = None                          # Process exit code if applicable
    transcript_snippet: str = ""                          # Brief excerpt of recent activity
    interruption_reason: str = ""                         # Why subagent was interrupted
    resource_limit_event: ResourceLimitEvent | None = None  # Resource limit that was hit
    continuation: RuntimeEngineContinuation | None = None   # Continuation context for resuming


SubagentRef = HeruSubagentRef


class RuntimeEngineSwitch(BaseModel):
    """Record of an engine switch during task execution.

    Tracks when and why the execution engine was changed during a task.
    Used for debugging engine routing decisions and understanding task
    execution patterns across different AI engines.
    """
    stage: str                                    # Pipeline stage where switch occurred
    from_engine: str                             # Previous engine
    to_engine: str                               # New engine
    reason: str                                  # Rationale for the engine switch
    happened_at: str = Field(default_factory=utcnow)  # When the switch occurred


class RuntimeHookRejectFingerprint(BaseModel):
    """Fingerprint of a hook rejection for detecting rejection loops.

    Used to track patterns of consecutive hook rejections and trigger
    recovery when the same type of hook failure happens repeatedly.
    Helps prevent infinite retry loops on persistent hook failures.
    """
    point: str          # Hook point where rejection occurred (before_*, after_*)
    command: str        # Hook command that was executed
    description: str = ""  # Brief description of the rejection
    fingerprint: str    # Computed fingerprint for detecting repeated rejections


class RuntimeRecoveryOutcome(BaseModel):
    """Compact recovery-attempt history persisted on the task runtime.

    Survives pipeline-state resets so later recovery turns can still see
    prior failure fingerprints for the same task.
    """

    origin_stage: str | None = None
    trigger_event_kind: str = ""
    fingerprint: str = ""
    classification: str | None = None
    budget_key: str = ""
    recovery_verdict: str = ""
    disposition: str = ""
    reason_code: str | None = None
    message: str = ""
    created_at: str | None = None


class TaskOutcomeState(BaseModel):
    """Final outcome state when a task completes or terminates.

    Captures the terminal result of task execution, including success,
    failure details, and context for follow-up actions. Used by
    reporting and recovery logic to understand task completion patterns.
    """
    kind: OutcomeKind | None = None                    # Terminal outcome category (flagged, blocked, etc.)
    stage: str | None = None                           # Pipeline stage where outcome was determined
    reason_code: OutcomeReasonCode | None = None       # Machine-readable reason for outcome
    reason: str = ""                                   # Human-readable explanation
    failure_classification: str | None = None          # Type of failure if applicable
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)  # Detailed failure context
    follow_up_task_id: str | None = None               # ID of any follow-up task created
    retry_count: int = 0                              # Number of retries attempted
    retry_limit: int = 0                              # Maximum retries allowed
    recorded_at: str | None = None                    # When outcome was recorded


class RuntimeInterruptionState(BaseModel):
    """State tracking for task execution interruptions.

    Records when and why a task was interrupted, along with context
    needed for potential resumption. Used by PipelineRunner to handle
    graceful interruption and by resume logic to restore execution state.
    """
    source: Literal["runner", "subagent"] = "runner"  # What initiated the interruption
    stage: str | None = None                          # Pipeline stage when interrupted
    pipeline_status: str | None = None                # Pipeline status when interrupted
    resume_stage: str | None = None                   # Stage to resume from if applicable
    reason: str = ""                                  # Why the interruption occurred
    summary: str = ""                                 # Brief description of interruption context
    interrupted_at: str | None = None                 # When interruption was initiated
    detected_at: str | None = None                    # When interruption was detected
    run_started_at: str | None = None                 # When the interrupted run started
    stage_started_at: str | None = None               # When the interrupted stage started
    subagent: RuntimeSubagentState | None = None


class RuntimeContinuationHandoff(BaseModel):
    """Context for resuming or continuing task execution.

    Provides the information needed to resume task execution after an
    interruption, retry, or engine switch. Different continuation kinds
    require different context:
    - retry: same engine/model, resume from failure point
    - engine_switch: different engine, may have session_token empty
    - restart: clean slate, minimal context preservation

    Used by PipelineRunner when resuming interrupted tasks.
    """
    stage: str                                       # Stage where continuation should occur
    kind: Literal["retry", "engine_switch", "restart"]  # Type of continuation
    reason: str                                      # Why continuation is needed
    from_engine: str | None = None                   # Previous engine if engine_switch
    to_engine: str | None = None                     # New engine if engine_switch
    from_model: str | None = None                    # Previous model if changing
    to_model: str | None = None                      # New model if changing
    subagent_id: str | None = None                   # Subagent to resume if applicable
    subagent_path: str | None = None                 # Path context for subagent
    status: str | None = None                        # Status context for continuation
    attempt: int | None = None
    summary: str = ""
    transcript_snippet: str = ""
    warnings: list[str] = Field(default_factory=list)
    session_path: str | None = None
    report_path: str | None = None
    transcript_path: str | None = None
    continuation: RuntimeEngineContinuation | None = None
    updated_at: str = Field(default_factory=utcnow)


class TaskRuntime(BaseModel):
    """The unified task-scoped container for mutable runtime state.

    Created by TaskService when a new task is created. Used by PipelineRunner,
    CLI, reporting, and recovery code for tracking execution progress.

    DESIGN RATIONALE - Unified vs. Split Architecture:

    The original target design called for splitting this into separate
    PipelineRuntime and ExecutionRuntime slices to separate concerns:
    - Pipeline slice: state, retries, current run details, recovery context
    - Execution slice: subagent state, interruption, continuation, engine switching

    However, the current implementation uses a unified design that combines both
    concerns into a single TaskRuntime class. This design choice provides:

    1. Simplified orchestration: PipelineRunner owns all runtime state management
       in one place instead of coordinating across multiple runtime objects
    2. Atomic state consistency: All runtime changes happen together, avoiding
       synchronization issues between pipeline and execution state
    3. Simplified persistence: Single object to load/save instead of managing
       multiple runtime slices

    OWNERSHIP PATHS:

    TaskRuntime is owned exclusively by PipelineRunner during execution:
    - PipelineRunner writes: All field updates during task execution
    - Other components read: CLI, reporting, recovery read current state

    TaskRuntime diverges from TaskState when:
    - TaskState tracks high-level pipeline position (stage, status, retry counts)
    - TaskRuntime tracks detailed execution state (subagents, interruptions, handoffs)
    - TaskState persists across task restarts; TaskRuntime may be reconstructed

    The execution orchestration keeps subagent launch, interruption, resume,
    and engine switching inside PipelineRunner, giving Litehive one orchestration
    service instead of splitting execution control across multiple services.
    """
    git: RuntimeGitState = Field(default_factory=RuntimeGitState)
    execution_status: str = "idle"
    run_started_at: str | None = None
    updated_at: str | None = None
    retry_count: int = 0
    retry_limit: int = 0
    stage_retry_counts: dict[str, int] = Field(default_factory=dict)
    current_stage: RuntimeStageState = Field(default_factory=RuntimeStageState)
    last_stage: RuntimeStageState = Field(default_factory=RuntimeStageState)
    active_subagent: RuntimeSubagentState | None = None
    last_subagent: RuntimeSubagentState | None = None
    interruption: RuntimeInterruptionState | None = None
    continuation_handoff: RuntimeContinuationHandoff | None = None
    last_engine_switch: RuntimeEngineSwitch | None = None
    consecutive_same_hook_rejects: int = 0
    last_hook_reject_fingerprint: RuntimeHookRejectFingerprint | None = None
    hook_reject_recovery_invoked: bool = False
    recovery_history: list[RuntimeRecoveryOutcome] = Field(default_factory=list)
    last_outcome: TaskOutcomeState = Field(default_factory=TaskOutcomeState)
    self_heal_traceback_fingerprints: list[str] = Field(default_factory=list)

    def for_storage(
        self,
        *,
        commit_sha: str | None,
        worktree_path: str | None,
    ) -> "TaskRuntime":
        runtime = self.model_copy(deep=True)
        runtime.git.commit_sha = commit_sha
        runtime.git.worktree_path = worktree_path
        return runtime


class RunnerStatusState(BaseModel):
    """Status tracking for the top-level task runner process.

    Provides monitoring information about the runner itself, independent
    of individual task state. Used by monitoring systems and operator
    interfaces to track runner health and current activity.
    """
    status: RunnerExecutionStatus = RunnerExecutionStatus.IDLE  # Current runner status (idle, running, late, stale)
    pid: int | None = None                                       # Process ID of the runner
    workspace: str = ""                                          # Current workspace path
    command: str = ""                                            # Command being executed if running
    started_at: str | None = None                                # When runner process started
    heartbeat_at: str | None = None                              # Last heartbeat timestamp
    active_task_id: str | None = None                            # ID of currently executing task
