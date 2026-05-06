"""
Per-task runtime state slices persisted alongside ``TaskRecord``.

``TaskRuntime`` splits the mutable execution data into two halves so
storage stays atomic per task while ownership is explicit:

- ``PipelineRuntime`` — projection of state-machine progress
  (state, retries, hook tracking, recovery and failed-run histories,
  last outcome). Owned by the lifecycle layer; lifecycle overwrites
  this slice after each transition. Non-lifecycle code may write
  closure/interruption outcomes but should not touch the rest.
- ``ExecutionRuntime`` — owned by the runner: which subagent is live,
  any interruption context, the most recent engine switch.

``RuntimeEngineContinuation`` itself lives in ``heru.types`` so the
runner and the engine client share one shape; we re-export it here for
convenience.
"""

from enum import Enum
from typing import Any, Literal, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from heru.types import (
    RuntimeEngineContinuation,
    SubagentRef as HeruSubagentRef,
)

from .common import (
    OutcomeKind,
    OutcomeReasonCode,
    RunnerExecutionStatus,
    SubagentStatus,
    TaskExecutionStatus,
    utcnow,
)


def _json_enum_value(value: object) -> object:
    """
    Pydantic JSON-serializer helper for enum-valued fields.

    Renders ``Enum`` members as their underlying value so persisted
    runtime JSON stays portable across renames; without this, a row
    written as ``RunnerStatus.IDLE`` would read back as the literal
    string ``"RunnerStatus.IDLE"`` and trip every consumer that
    expects ``"idle"``.
    """
    if isinstance(value, Enum):
        return value.value
    return value


class RuntimeGitState(BaseModel):
    """
    Per-task git context (commit + worktree) the runner is operating in.

    Updated by ``PipelineRunner`` as commits land and the worktree is
    sync'd. Persisted on ``PipelineRuntime`` so status output and the
    daemon's worktree gate can answer "where is this task right now?"
    without re-reading git on every status call.
    """

    commit_sha: str | None = None  # Current git commit SHA being worked on
    worktree_path: str | None = None  # Path to the git worktree if using worktrees


class RuntimeStageState(BaseModel):
    """
    Snapshot of which stage is running and for how long.

    The runner overwrites this after every stage transition so status
    output and the operator's CLI can render "currently in
    implementing for 42s" without consulting the lifecycle log. Stored
    on ``PipelineRuntime.current_stage``.
    """

    stage: str | None = None  # Pipeline stage being executed
    status: str = "idle"  # Execution status (idle, running, completed, failed)
    started_at: str | None = None  # When stage execution started
    updated_at: str | None = None  # Last status update timestamp
    duration_seconds: int = 0  # How long the stage has been running

    def model_copy(self, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """
        Override pydantic's ``model_copy`` to drop unknown ``update`` keys.

        Lifecycle code passes generic stage-update dicts assembled from
        events; without this filter, every newly-introduced field on
        the event side would have to be added here too or break
        existing callers. Filtering at the model boundary keeps the
        receiver tolerant to forward-rev callers.
        """
        if update is not None:
            update = {key: value for key, value in update.items() if key in type(self).model_fields}
        return super().model_copy(update=update, deep=deep)


class RuntimeSubagentState(BaseModel):
    """
    Subagent execution snapshot stored on the active task.

    Carries the live identifiers (``id``, ``role``, ``engine``, ``pid``)
    plus a short ``execution_trace_snippet`` so status output can name
    the running agent without paging in the full transcript artifact.
    Created when the runner spawns a subagent, finalized when it
    completes or is interrupted; full logs and traces stay in the
    artifact directory, not on this object.
    """

    id: str  # Unique subagent identifier
    role: str  # Execution role (planner, swe, qa, etc.)
    engine: str  # AI engine being used
    status: SubagentStatus  # Current lifecycle status
    path: str  # Filesystem path where subagent runs
    pid: int | None = None  # Process ID if applicable
    sandboxed: bool = False  # Whether running in sandbox
    sandbox_summary: str = ""  # Brief sandbox status
    started_at: str  # Subagent start timestamp
    updated_at: str  # Last status update timestamp
    completed_at: str | None = None  # Completion timestamp if finished
    exit_code: int | None = None  # Process exit code if applicable
    execution_trace_snippet: str = ""  # Brief execution-trace excerpt
    interruption_reason: str = ""  # Why subagent was interrupted
    continuation: RuntimeEngineContinuation | None = None  # Continuation context for resuming


SubagentRef = HeruSubagentRef


class RuntimeEngineSwitch(BaseModel):
    """
    Bookkeeping for the most recent engine switch on a task.

    Recovery and routing logic occasionally swap the engine mid-task
    (e.g. fall back to a smaller engine after a budget hit); this
    record explains *what* changed and *why* so the operator's
    timeline can show the switch alongside the rest of the run.
    Persisted on ``ExecutionRuntime.last_engine_switch``.
    """

    stage: str  # Pipeline stage where switch occurred
    from_engine: str  # Previous engine
    to_engine: str  # New engine
    reason: str  # Rationale for the engine switch
    happened_at: str = Field(default_factory=utcnow)  # When the switch occurred


class RuntimeHookRejectFingerprint(BaseModel):
    """
    Persisted shape of a hook rejection used for loop detection.

    Compared against the next hook reject to decide whether the same
    hook is firing repeatedly; once the consecutive count crosses
    ``state.limits.same_hook_reject_limit`` the lifecycle layer
    escalates to recovery instead of retrying. Mirrors
    ``HookRejectFingerprint`` on lifecycle state.
    """

    point: str  # Hook point where rejection occurred (before_*, after_*)
    command: str  # Hook command that was executed
    description: str = ""  # Brief description of the rejection
    fingerprint: str  # Computed fingerprint for detecting repeated rejections


class RuntimeRecoveryOutcome(BaseModel):
    """
    Compact projection of ``RecoveryOutcome`` stored on runtime for display.

    ``TaskState.recovery_history`` owns the authoritative
    ``RecoveryOutcome`` list; this flat copy exists so status and
    prompt surfaces can render recovery history without joining
    through the lifecycle store. Mutated only via the lifecycle
    projection path — direct writes here would let runtime drift from
    history.
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
    """
    Compact projection of ``FailedRunRecord`` exposed on runtime.

    ``TaskState.failed_run_history`` owns the mutable record; this
    runtime copy backs the queue's "this task already exhausted its
    budget on this shape" check and the operator's status view.
    Updated only by the lifecycle projection path or by an explicit
    operator-ack — never by hot-path runner code.
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
    """
    Terminal outcome record stored on runtime when a task ends.

    Captures the kind of outcome, where it happened, and a
    human-readable reason so post-mortem and reporting code can render
    a consistent end-of-run summary without consulting the lifecycle
    log. ``failure_diagnostics`` here is report evidence copied for
    visibility; recovery identity (fingerprint, classification, budget
    key) lives on ``RecoveryTrigger`` in the recovery domain.
    """

    model_config = ConfigDict(validate_assignment=True)

    kind: OutcomeKind | None = None  # Terminal outcome category (flagged, blocked, etc.)
    stage: str | None = None  # Pipeline stage where outcome was determined
    reason_code: OutcomeReasonCode | None = None  # Machine-readable reason for outcome
    reason: str = ""  # Human-readable explanation
    failure_classification: str | None = None  # Type of failure if applicable
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)  # Report evidence
    follow_up_task_id: str | None = None  # ID of any follow-up task created
    retry_count: int = 0  # Number of retries attempted
    retry_limit: int = 0  # Maximum retries allowed
    recorded_at: str | None = None  # When outcome was recorded

    @field_serializer("kind", "reason_code", when_used="json")
    def _serialize_runtime_enum_value(self, value: object) -> object:
        """
        Flatten ``OutcomeKind`` / ``OutcomeReasonCode`` enums to their string values.

        Without this, persisted runtime JSON would carry
        ``OutcomeKind.DONE`` literal text and break every consumer
        that filters by the canonical string. Same shape as
        ``_json_enum_value`` above; declared here so pydantic wires it
        as the field serializer for these specific fields.
        """
        return _json_enum_value(value)


class RuntimeInterruptionState(BaseModel):
    """
    Interruption context the runner persists when a task pauses.

    Captures both *what* was interrupted (stage, pipeline status,
    optional active subagent) and *what to resume* (``resume_stage``,
    timestamps for "how long was it stalled"). Without this snapshot
    the recovery layer cannot tell a resumable interruption from a
    crash that needs full re-entry.
    """

    source: Literal["runner", "subagent"] = "runner"  # What initiated the interruption
    stage: str | None = None  # Pipeline stage when interrupted
    pipeline_status: str | None = None  # Pipeline status when interrupted
    resume_stage: str | None = None  # Stage to resume from if applicable
    reason: str = ""  # Why the interruption occurred
    summary: str = ""  # Brief description of interruption context
    interrupted_at: str | None = None  # When interruption was initiated
    detected_at: str | None = None  # When interruption was detected
    run_started_at: str | None = None  # When the interrupted run started
    stage_started_at: str | None = None  # When the interrupted stage started
    subagent: RuntimeSubagentState | None = None


class PipelineRuntime(BaseModel):
    """
    Lifecycle projection persisted alongside the task.

    Authoritative state-machine data lives on ``TaskState``; this slice
    is the read-friendly mirror for status, queue filtering, and
    prompt context. Lifecycle orchestration overwrites it after every
    transition. Non-lifecycle task operations may still write
    closure/interruption outcomes here, which is why those fields are
    on this slice rather than on ``ExecutionRuntime``.
    """

    git: RuntimeGitState = Field(default_factory=RuntimeGitState)
    execution_status: TaskExecutionStatus | str = TaskExecutionStatus.IDLE
    run_started_at: str | None = None
    updated_at: str | None = None
    retry_count: int = 0
    retry_limit: int = 0
    current_stage: RuntimeStageState = Field(default_factory=RuntimeStageState)
    consecutive_same_hook_rejects: int = 0
    last_hook_reject_fingerprint: RuntimeHookRejectFingerprint | None = None
    hook_reject_recovery_invoked: bool = False
    recovery_history: list[RuntimeRecoveryOutcome] = Field(default_factory=list)
    failed_run_history: dict[str, RuntimeFailedRunRecord] = Field(default_factory=dict)
    last_outcome: TaskOutcomeState = Field(default_factory=TaskOutcomeState)

    @field_serializer("execution_status", when_used="json")
    def _serialize_execution_status(self, value: object) -> object:
        """
        Persist the task execution marker as its stable string spelling.
        """
        return _json_enum_value(value)


class ExecutionRuntime(BaseModel):
    """
    Subagent-execution slice of the task runtime.

    Owned by the runner: which subagent is currently live, the most
    recent interruption context (so resume can restore stage and
    started-at timestamps), and the latest engine-switch record.
    Splitting this off ``PipelineRuntime`` keeps lifecycle and runner
    writes from stepping on each other's slice during concurrent
    updates.
    """

    active_subagent: RuntimeSubagentState | None = None
    interruption: RuntimeInterruptionState | None = None
    last_engine_switch: RuntimeEngineSwitch | None = None


class TaskRuntime(BaseModel):
    """
    Task-scoped runtime split by ownership: pipeline + execution.

    Keeping the persistence boundary atomic per task (one ``TaskRuntime``
    per row) while letting lifecycle and runner each own their slice
    avoids the bridge-code pattern where two modules sync the same
    fields back and forth — see code-style "State And Ownership". The
    two slices serialize together but are written independently.
    """

    model_config = ConfigDict(extra="forbid")

    pipeline: PipelineRuntime = Field(default_factory=PipelineRuntime)
    execution: ExecutionRuntime = Field(default_factory=ExecutionRuntime)

    def for_storage(
        self,
        commit_sha: str | None,
        worktree_path: str | None,
    ) -> "TaskRuntime":
        """
        Return a deep copy with the current git context stamped in.

        Called right before persistence so the saved runtime row
        always carries the commit/worktree the task was at, even when
        the live in-memory ``TaskRuntime`` has not yet seen the commit
        propagate. Without this, status output could read back the
        stale git context for a freshly-committed task.
        """
        runtime = self.model_copy(deep=True)
        runtime.pipeline.git.commit_sha = commit_sha
        runtime.pipeline.git.worktree_path = worktree_path
        return runtime


class RunnerStatusState(BaseModel):
    """
    Heartbeat-driven snapshot of the workspace's runner process.

    Independent of any single task: tracks the runner's PID, current
    workspace, last heartbeat, and currently-active task id. The
    daemon's pre-spawn check reads this to refuse starting a second
    runner; ``litehive status`` reads it to render the runner-health
    line.
    """

    model_config = ConfigDict(validate_assignment=True)

    status: RunnerExecutionStatus = RunnerExecutionStatus.IDLE  # Current runner status (idle, running, late, stale)
    pid: int | None = None  # Process ID of the runner
    workspace: str = ""  # Current workspace path
    command: str = ""  # Command being executed if running
    started_at: str | None = None  # When runner process started
    heartbeat_at: str | None = None  # Last heartbeat timestamp
    active_task_id: str | None = None  # ID of currently executing task

    @field_serializer("status", when_used="json")
    def _serialize_status(self, value: object) -> object:
        """
        JSON serializer for ``RunnerExecutionStatus``.

        Status surfaces and the on-disk heartbeat row consume the bare
        string ``"idle"`` rather than ``RunnerExecutionStatus.IDLE``;
        without this serializer the daemon would write the latter and
        the status reader would compare strings against the wrong
        spelling.
        """
        return _json_enum_value(value)
