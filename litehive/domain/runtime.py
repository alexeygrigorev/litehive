"""Runtime and execution state models.

RuntimeEngineContinuation now lives in heru.types. This module re-exports it
and keeps the litehive-only runtime state models authoritative here.
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from heru.types import (
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
    """Compact projection of a ``RecoveryOutcome`` persisted on task runtime.

    Survives pipeline-state resets so later recovery turns can still see
    prior failure fingerprints for the same task. This is not a second
    recovery model: ``TaskState.recovery_history`` owns full
    ``RecoveryOutcome`` objects, while ``PipelineRuntime.recovery_history``
    keeps this compact history for prompts and status surfaces.
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


class RuntimeFailedRunRecord(BaseModel):
    """Cross-run memory for terminal stage retry exhaustion.

    This lives on the task runtime so it survives v2 pipeline state resets
    performed by manual requeue and stale/flagged-task recovery paths.
    """

    stage: str
    failure_shape: str
    count: int = 0
    first_at: str | None = None
    latest_at: str | None = None
    last_reason: str = ""
    source: str | None = None
    classification: str | None = None
    retry_limit: int | None = None
    failed_reason: str | None = None
    operator_override_count: int = 0
    last_operator_override_at: str | None = None


class TaskOutcomeState(BaseModel):
    """Final outcome state when a task completes or terminates.

    Captures the terminal result of task execution, including success,
    failure details, and context for follow-up actions. Used by
    reporting and recovery logic to understand task completion patterns.

    ``failure_diagnostics`` is report evidence copied into the terminal
    outcome for operator/debug visibility. Recovery identity and budget
    tracking belong to ``FailureFingerprint`` on ``RecoveryTrigger``.
    """
    kind: OutcomeKind | None = None                    # Terminal outcome category (flagged, blocked, etc.)
    stage: str | None = None                           # Pipeline stage where outcome was determined
    reason_code: OutcomeReasonCode | None = None       # Machine-readable reason for outcome
    reason: str = ""                                   # Human-readable explanation
    failure_classification: str | None = None          # Type of failure if applicable
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)  # Report evidence
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


class PipelineRuntime(BaseModel):
    """Mutable runtime state owned by the task pipeline.

    This slice tracks run status, stage progress, retry accounting, terminal
    outcomes, and recovery memory. It deliberately excludes subagent and
    continuation bookkeeping, which belongs to ExecutionRuntime.

    Recovery memory here mirrors the canonical state-machine vocabulary:
    ``recovery_history`` stores compact ``RuntimeRecoveryOutcome`` projections
    of ``RecoveryOutcome`` objects, and ``failed_run_history`` stores separate
    terminal retry-exhaustion records.
    """

    git: RuntimeGitState = Field(default_factory=RuntimeGitState)
    execution_status: str = "idle"
    run_started_at: str | None = None
    updated_at: str | None = None
    retry_count: int = 0
    retry_limit: int = 0
    current_stage: RuntimeStageState = Field(default_factory=RuntimeStageState)
    last_stage: RuntimeStageState = Field(default_factory=RuntimeStageState)
    consecutive_same_hook_rejects: int = 0
    last_hook_reject_fingerprint: RuntimeHookRejectFingerprint | None = None
    hook_reject_recovery_invoked: bool = False
    recovery_history: list[RuntimeRecoveryOutcome] = Field(default_factory=list)
    failed_run_history: dict[str, RuntimeFailedRunRecord] = Field(default_factory=dict)
    last_outcome: TaskOutcomeState = Field(default_factory=TaskOutcomeState)


class ExecutionRuntime(BaseModel):
    """Mutable runtime state owned by subagent execution.

    This slice tracks active/recent subagent state plus interruption,
    continuation, and engine-switch context used to resume or redirect work.
    """

    active_subagent: RuntimeSubagentState | None = None
    last_subagent: RuntimeSubagentState | None = None
    interruption: RuntimeInterruptionState | None = None
    continuation_handoff: RuntimeContinuationHandoff | None = None
    last_engine_switch: RuntimeEngineSwitch | None = None


_PIPELINE_RUNTIME_FIELDS = frozenset(PipelineRuntime.model_fields)
_EXECUTION_RUNTIME_FIELDS = frozenset(ExecutionRuntime.model_fields)


def _model_or_mapping_payload(value: object) -> dict:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, dict):
        return dict(value)
    return {}


class TaskRuntime(BaseModel):
    """Task-scoped runtime container split by ownership boundary.

    TaskRuntime keeps persistence atomic for a task while separating mutable
    runtime state into:
    - pipeline: run status, stage progress, retries, outcomes, and recovery
    - execution: subagents, interruptions, handoffs, and engine switching

    Legacy flat runtime payloads are accepted during validation and normalized
    into these slices before storage.
    """

    pipeline: PipelineRuntime = Field(default_factory=PipelineRuntime)
    execution: ExecutionRuntime = Field(default_factory=ExecutionRuntime)

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_flat_payload(cls, data):
        if isinstance(data, TaskRuntime):
            return data
        if not isinstance(data, dict):
            return data

        payload = dict(data)
        pipeline_payload = _model_or_mapping_payload(payload.get("pipeline"))
        execution_payload = _model_or_mapping_payload(payload.get("execution"))

        for field_name in _PIPELINE_RUNTIME_FIELDS:
            if field_name in payload:
                pipeline_payload[field_name] = payload.pop(field_name)
        for field_name in _EXECUTION_RUNTIME_FIELDS:
            if field_name in payload:
                execution_payload[field_name] = payload.pop(field_name)

        payload["pipeline"] = pipeline_payload
        payload["execution"] = execution_payload
        return payload

    def __getattr__(self, name: str):
        if name in _PIPELINE_RUNTIME_FIELDS:
            return getattr(self.pipeline, name)
        if name in _EXECUTION_RUNTIME_FIELDS:
            return getattr(self.execution, name)
        return super().__getattr__(name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in _PIPELINE_RUNTIME_FIELDS and name not in type(self).model_fields:
            setattr(self.pipeline, name, value)
            return
        if name in _EXECUTION_RUNTIME_FIELDS and name not in type(self).model_fields:
            setattr(self.execution, name, value)
            return
        super().__setattr__(name, value)

    def for_storage(
        self,
        *,
        commit_sha: str | None,
        worktree_path: str | None,
    ) -> "TaskRuntime":
        runtime = self.model_copy(deep=True)
        runtime.pipeline.git.commit_sha = commit_sha
        runtime.pipeline.git.worktree_path = worktree_path
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
