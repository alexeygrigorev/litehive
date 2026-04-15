# Vocabulary

This document defines the desired domain model and canonical vocabulary for
Litehive.

It is normative:

- use one canonical term per concept
- do not use `v2` in user-facing language
- do not use bare overloaded words like `status`, `state`, `reason`, or
  `outcome` when more than one kind is in scope
- group related concepts by domain so review is local, not scattered

## Naming Rules

- Use `task` for the core work item.
- Use `pipeline` for the task execution flow.
- Use `lifecycle` for the implementation mechanics of that flow.
- Use `message` for human-readable text.
- Use `reason_code` for normalized machine-readable classification.
- Use `rationale` for operator or agent explanation of a choice.

## Workspace Domain

### Workspace

The repository root managed by Litehive.

- Preferred: `workspace`
- Python type: `Path`

```python
workspace: Path
```

### Workspace Queue

The ordered list of tasks waiting to run in one workspace.

- Preferred: `task queue`
- Python type: `TaskQueue`

```python
class TaskQueue(BaseModel):
    active_task_id: str | None = None
    queued_task_ids: list[str] = Field(default_factory=list)
```

### Workspace Runtime Store

The durable persistence API for workspace runtime data.

- Preferred: `workspace runtime store`
- Python type: `WorkspaceRuntimeStore`

```python
class WorkspaceRuntimeStore(Protocol):
    def load_task(self, task_id: str) -> Task | None: ...
    def save_task(self, task: Task) -> None: ...
    def load_queue(self) -> TaskQueue: ...
    def save_queue(self, queue: TaskQueue) -> None: ...
```

## Task Domain

### Task Status

The high-level execution or terminal category for a task.

- Preferred: `task status`
- Python type: `TaskStatus`

```python
class TaskStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    INTERRUPTED = "interrupted"
    PARKED = "parked"
    DONE = "done"
    FLAGGED = "flagged"
    CANCELLED = "cancelled"
    CLOSED = "closed"
```

Values:

- `queued`: waiting in the queue
- `in_progress`: currently executing
- `interrupted`: execution stopped and can potentially resume
- `parked`: intentionally paused
- `done`: completed successfully
- `flagged`: requires explicit operator attention
- `cancelled`: explicitly cancelled
- `closed`: intentionally closed without completion

### Task Close Reason

The explicit operator-chosen reason when closing a task.

- Preferred: `task close reason`
- Preferred field name: `close_reason`
- Python type: `TaskCloseReason`

```python
class TaskCloseReason(str, Enum):
    WONT_DO = "wont_do"
    DEFERRED = "deferred"
    DUPLICATE = "duplicate"
```

### Task Flag Reason

The normalized reason a task was flagged.

- Preferred: `task flag reason`
- Preferred field name: `flag_reason`
- Python type: `TaskFlagReason`

```python
class TaskFlagReason(str, Enum):
    MERGE_FAILED = "merge_failed"
    RECOVERY_FAILED = "recovery_failed"
    RECOVERY_BUDGET_EXCEEDED = "recovery_budget_exceeded"
    OPERATOR_ATTENTION_REQUIRED = "operator_attention_required"
```

### Task Priority

The scheduling priority of a task.

- Preferred: `task priority`
- Python type: `TaskPriority`

```python
class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
```

### Task Type

The optional classification of a task for planning or routing.

- Preferred: `task type`
- Python type: `TaskType`

```python
class TaskType(str, Enum):
    FEATURE = "feature"
    BUG = "bug"
    CHORE = "chore"
    REFACTOR = "refactor"
```

### Task Retry Policy

The configured retry limits for a task.

- Preferred: `task retry policy`
- Python type: `TaskRetryPolicy`

```python
class TaskRetryPolicy(BaseModel):
    max_retries: int | None = None
    stage_retry_limit: int | None = None
```

### Task Git State

The git-related state attached to a task.

- Preferred: `task git state`
- Python type: `TaskGitState`

```python
class TaskGitState(BaseModel):
    auto_commit: bool = True
    commit_message: str | None = None
    commit_sha: str | None = None
    checkpoint_base_sha: str | None = None
    checkpoint_attempts: int = 0
    rolled_back_checkpoint_attempt: int | None = None
    merge_attempts: int = 0
    worktree_path: Path | None = None
```

### Task

A unit of work tracked by Litehive.

- Preferred: `task`
- Python type: `Task`

```python
class Task(BaseModel):
    id: str
    slug: str
    title: str
    task_type: TaskType | None = None
    model: str | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    status: TaskStatus = TaskStatus.QUEUED
    flag_reason: TaskFlagReason | None = None
    created_at: datetime
    updated_at: datetime
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    git: TaskGitState = Field(default_factory=TaskGitState)
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)
    runtime_state: TaskRuntimeState = Field(default_factory=lambda: TaskRuntimeState())
```

Field meanings:

- `goal`: the main intended result
- `acceptance_criteria`: concrete completion conditions
- `constraints`: limitations or rules that must be respected
- `plan`: current execution plan
- `depends_on`: upstream task ids

## Pipeline Domain

### Pipeline Mode

The top-level execution mode for a task.

- Preferred: `pipeline mode`
- Python type: `PipelineMode`

```python
class PipelineMode(str, Enum):
    FULL = "full"
    SINGLE = "single"
```

### Task Stage

A user-facing major step in task execution.

- Preferred: `task stage`
- Short form when the context is obvious: `stage`
- Python type: `TaskStage`

```python
class TaskStage(str, Enum):
    GROOMING = "grooming"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    ACCEPTING = "accepting"
    COMMIT_TO_GIT = "commit_to_git"
    RECOVERING = "recovering"
```

### Pipeline State

The full internal machine state for task execution.

- Preferred: `pipeline state`
- Avoid calling this just `stage`
- Python type: `PipelineState`

```python
class PipelineState(str, Enum):
    READY = "ready"
    WORKTREE_SYNC = "worktree_sync"
    BEFORE_GROOMING = "before_grooming"
    GROOMING = "grooming"
    AFTER_GROOMING = "after_grooming"
    BEFORE_IMPLEMENTING = "before_implementing"
    IMPLEMENTING = "implementing"
    AFTER_IMPLEMENTING = "after_implementing"
    BEFORE_TESTING = "before_testing"
    TESTING = "testing"
    AFTER_TESTING = "after_testing"
    BEFORE_ACCEPTING = "before_accepting"
    ACCEPTING = "accepting"
    AFTER_ACCEPTING = "after_accepting"
    BEFORE_COMMIT = "before_commit"
    COMMIT = "commit"
    AFTER_COMMIT = "after_commit"
    MERGE_RESOLVING = "merge_resolving"
    RECOVERING_PRE_EXEC = "recovering_pre_exec"
    RECOVERING = "recovering"
    DONE = "done"
    FAILED = "failed"
```

### Pipeline State View

The projected task-facing view of pipeline progress.

- Preferred: `pipeline state view`
- This intentionally replaces the confusing separate term `pipeline status`
- Python type: `PipelineStateView`

```python
class PipelineStateView(str, Enum):
    BACKLOG = "backlog"
    GROOMING = "grooming"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    ACCEPTING = "accepting"
    COMMIT_TO_GIT = "commit_to_git"
    DONE = "done"
    FLAGGED = "flagged"
```

### Stage Run Status

The runtime status of one task stage attempt.

- Preferred: `stage run status`
- Python type: `StageRunStatus`

```python
class StageRunStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
```

### Stage Verdict

The decision submitted for a task stage.

- Preferred: `stage verdict`
- Short form when the context is obvious: `verdict`
- Python type: `StageVerdict`

```python
class StageVerdict(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    BLOCKED = "blocked"
```

Why `blocked` is separate from `reject`:

- `reject` means Litehive can continue autonomously
- `blocked` means progress requires external input or operator decision

### Stage Execution State

The runtime state of one stage attempt.

- Preferred: `stage execution state`
- Python type: `StageExecutionState`

```python
class StageExecutionState(BaseModel):
    task_stage: TaskStage | None = None
    status: StageRunStatus = StageRunStatus.IDLE
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None
    duration_seconds: int = 0
    verdict: StageVerdict | None = None
    summary: str = ""
```

### Lifecycle Node

The executable object that owns behavior for a pipeline state.

- Preferred: `lifecycle node`
- Short form when the context is obvious: `node`
- Python type: `LifecycleNode`

```python
class LifecycleNode(ABC):
    @abstractmethod
    def run(self, task: Task, pipeline_state: PipelineState) -> LifecycleEvent: ...
```

### Lifecycle Event Source

The origin of a lifecycle event when that distinction matters.

- Preferred: `lifecycle event source`
- Python type: `LifecycleEventSource`

```python
class LifecycleEventSource(str, Enum):
    AGENT = "agent"
    HOOK = "hook"
    GUARD = "guard"
    SYSTEM = "system"
```

### Lifecycle Event

A typed signal emitted by a lifecycle node or runner and consumed by the
transition rules.

- Preferred: `lifecycle event`
- Short form when the context is obvious: `event`
- Avoid `runtime event`
- Python type: `LifecycleEvent`

```python
@dataclass(frozen=True)
class LifecycleEvent:
    pass


@dataclass(frozen=True)
class AcceptEvent(LifecycleEvent):
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RejectEvent(LifecycleEvent):
    source: LifecycleEventSource
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BlockedEvent(LifecycleEvent):
    message: str


@dataclass(frozen=True)
class CrashEvent(LifecycleEvent):
    exc_type: str
    message: str


@dataclass(frozen=True)
class TimeoutEvent(LifecycleEvent):
    message: str = ""
```

## Recovery Domain

### Trigger Event Kind

The normalized label for the event that triggered recovery.

- Preferred field name: `trigger_event_kind`
- Python type: `TriggerEventKind`

```python
class TriggerEventKind(str, Enum):
    CRASH = "crash"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    REJECT = "reject"
    RETRY_LIMIT = "retry_limit"
    STAGE_RETRY_LIMIT = "stage_retry_limit"
    PRE_EXEC_RECOVERY_FAILED = "pre_exec_recovery_failed"
    MERGE_CONFLICT = "merge_conflict"
```

### Recovery Diagnostics

Structured diagnostics captured for recovery or recovery-trigger analysis.

- Preferred: `recovery diagnostics`
- Python type: `RecoveryDiagnostics`

```python
class RecoveryDiagnostics(BaseModel):
    values: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)
```

### Recovery Trigger

The structured input that explains why Litehive entered recovery.

- Preferred: `recovery trigger`
- Python type: `RecoveryTrigger`

```python
class RecoveryTrigger(BaseModel):
    origin_stage: TaskStage
    trigger_event_kind: TriggerEventKind
    message: str = ""
    diagnostics: RecoveryDiagnostics = Field(default_factory=RecoveryDiagnostics)
```

### Recovery Result

The result category of one recovery attempt.

- Preferred: `recovery result`
- This replaces the more awkward term `recovery disposition`
- Python type: `RecoveryResult`

```python
class RecoveryResult(str, Enum):
    RESUMED = "resumed"
    ADVANCED = "advanced"
    DONE = "done"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
```

### Recovery Evidence

One evidence item collected during recovery.

- Preferred: `recovery evidence`
- Python type: `RecoveryEvidence`

```python
class RecoveryEvidence(BaseModel):
    kind: str
    label: str
    path: Path | None = None
    exists: bool = False
    summary: str = ""
    metadata: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)
```

### Recovery Action

One action applied during recovery.

- Preferred: `recovery action`
- Python type: `RecoveryAction`

```python
class RecoveryAction(BaseModel):
    kind: str
    applied: bool = True
    summary: str = ""
    metadata: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)
```

### Recovery Outcome

The persisted result of one recovery attempt.

- Preferred: `recovery outcome`
- Python type: `RecoveryOutcome`

```python
class RecoveryOutcome(BaseModel):
    task_id: str
    trigger: RecoveryTrigger
    result: RecoveryResult
    summary: str
    message: str = ""
    blocker: str | None = None
    evidence: list[RecoveryEvidence] = Field(default_factory=list)
    actions: list[RecoveryAction] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
```

### Recovery Context

The recovery-entry payload carried into runtime state and prompts.

- Preferred: `recovery context`
- Python type: `RecoveryContext`

```python
class RecoveryContext(BaseModel):
    trigger: RecoveryTrigger
    last_rejection: VerdictEntry | None = None
    diagnostics: RecoveryDiagnostics = Field(default_factory=RecoveryDiagnostics)
```

## Execution Domain

### Runner Status

The execution status of the top-level Litehive runner process.

- Preferred: `runner status`
- Python type: `RunnerStatus`

```python
class RunnerStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    LATE = "late"
    STALE = "stale"
```

### Runtime Git State

The runtime git state attached to task execution.

- Preferred: `runtime git state`
- Python type: `RuntimeGitState`

```python
class RuntimeGitState(BaseModel):
    commit_sha: str | None = None
    worktree_path: Path | None = None
```

### Continuation Handle

The engine-specific resume handle that allows a later run to continue an
existing session.

- Preferred: `continuation handle`
- Python type: `ContinuationHandle`

```python
class ContinuationHandle(BaseModel):
    value: str
```

### Subagent Status

The runtime status of one subagent execution.

- Preferred: `subagent status`
- Python type: `SubagentStatus`

```python
class SubagentStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
```

### Execution Role

The role a subagent is performing.

- Preferred: `execution role`
- Python type: `ExecutionRole`

```python
class ExecutionRole(str, Enum):
    PLANNER = "planner"
    SWE = "swe"
    QA = "qa"
    REVIEWER = "reviewer"
    RECOVERY = "recovery"
    MERGE_RESOLVER = "merge-resolver"
```

### Subagent Execution State

The runtime state of one subagent run.

- Preferred: `subagent execution state`
- Python type: `SubagentExecutionState`

```python
class SubagentExecutionState(BaseModel):
    id: str
    role: ExecutionRole
    engine: EngineName
    status: SubagentStatus
    path: Path
    pid: int | None = None
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    exit_code: int | None = None
    sandboxed: bool = False
    sandbox_summary: str = ""
    execution_trace_snippet: str = ""
    interruption_reason: str = ""
    continuation: ContinuationHandle | None = None
```

### Interruption State

The runtime state describing an interruption.

- Preferred: `interruption state`
- Python type: `InterruptionState`

```python
class InterruptionState(BaseModel):
    source: Literal["runner", "subagent"] = "runner"
    task_stage: TaskStage | None = None
    message: str = ""
    interrupted_at: datetime | None = None
    subagent: SubagentExecutionState | None = None
```

### Engine Switch

The record of switching execution from one engine to another.

- Preferred: `engine switch`
- Python type: `EngineSwitch`

```python
class EngineSwitch(BaseModel):
    task_stage: TaskStage
    from_engine: EngineName
    to_engine: EngineName
    rationale: str
    happened_at: datetime
```

### Continuation Handoff

The durable record passed to a later run so it can resume execution.

- Preferred: `continuation handoff`
- Python type: `ContinuationHandoff`

```python
class ContinuationHandoff(BaseModel):
    task_stage: TaskStage
    kind: Literal["retry", "engine_switch", "restart"]
    from_engine: EngineName | None = None
    to_engine: EngineName | None = None
    continuation: ContinuationHandle | None = None
    summary: str = ""
    updated_at: datetime
```

### Task Outcome

The latest recorded execution outcome for a task.

- Preferred: `task outcome`
- Python type: `TaskOutcome`

```python
class TaskOutcome(BaseModel):
    task_stage: TaskStage | None = None
    reason_code: str | None = None
    message: str = ""
    retry_count: int = 0
    retry_limit: int = 0
    recorded_at: datetime | None = None
```

### Task Runtime State

The mutable execution state attached to a task while Litehive runs.

- Preferred: `task runtime state`
- Python type: `TaskRuntimeState`

```python
class TaskRuntimeState(BaseModel):
    git: RuntimeGitState = Field(default_factory=RuntimeGitState)
    execution_status: StageRunStatus = StageRunStatus.IDLE
    run_started_at: datetime | None = None
    updated_at: datetime | None = None
    retry_count: int = 0
    retry_limit: int = 0
    stage_retry_counts: dict[TaskStage, int] = Field(default_factory=dict)
    current_stage: StageExecutionState = Field(default_factory=StageExecutionState)
    last_stage: StageExecutionState = Field(default_factory=StageExecutionState)
    active_subagent: SubagentExecutionState | None = None
    last_subagent: SubagentExecutionState | None = None
    interruption: InterruptionState | None = None
    continuation_handoff: ContinuationHandoff | None = None
    last_engine_switch: EngineSwitch | None = None
    recovery_context: RecoveryContext | None = None
    last_outcome: TaskOutcome = Field(default_factory=TaskOutcome)
```

## Activity and Reports Domain

### Activity Entry

One append-only entry attached to a task.

- Preferred: `activity entry`
- Acceptable legacy term when discussing old storage only: `comment`
- Python type: `ActivityEntry`

```python
class ActivityEntry(BaseModel):
    role: str
    task_stage: TaskStage | None = None
    message: str
    created_at: datetime
```

### Verdict Entry

An activity entry that carries a stage verdict.

- Preferred: `verdict entry`
- Python type: `VerdictEntry`

```python
class VerdictEntry(ActivityEntry):
    task_stage: TaskStage
    verdict: StageVerdict
    files_changed: list[str] = Field(default_factory=list)
```

### Task Activity

The append-only history attached to a task.

- Preferred: `task activity`
- Python type: `TaskActivity`

```python
class TaskActivity(BaseModel):
    task_id: str
    entries: list[ActivityEntry] = Field(default_factory=list)
```

### Outcome Reason Code

A normalized reason code for stage outcomes.

- Preferred: `outcome reason code`
- Python type: `OutcomeReasonCode`

```python
class OutcomeReasonCode(str, Enum):
    VERDICT_REJECT = "verdict_reject"
    VERDICT_BLOCKED = "verdict_blocked"
    EXECUTION_INTERRUPTED = "execution_interrupted"
    MERGE_CONFLICT = "merge_conflict"
```

### Failure Reason Code

A normalized reason code for terminal failures.

- Preferred: `failure reason code`
- Python type: `FailureReasonCode`

```python
class FailureReasonCode(str, Enum):
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    RECOVERY_BUDGET_EXCEEDED = "recovery_budget_exceeded"
    RECOVERY_CRASHED = "recovery_crashed"
    PRE_EXEC_RECOVERY_FAILED = "pre_exec_recovery_failed"
```

### Failure Fingerprint

A stable normalized identifier used to decide whether two failures are
materially the same.

- Preferred: `failure fingerprint`
- Python type: `FailureFingerprint`

```python
class FailureFingerprint(BaseModel):
    kind: str
    fingerprint: str
```

### Stage Report

A structured summary of what happened in one task stage run.

- Preferred: `stage report`
- Python type: `StageReport`

```python
class StageReport(BaseModel):
    task_id: str
    task_stage: TaskStage
    stage_verdict: StageVerdict
    source: Literal["agent", "hook"] = "agent"
    summary: str
    message: str = ""
    files_changed: list[str] = Field(default_factory=list)
    tests_added: int = 0
    tests_passing: int = 0
    warnings: list[str] = Field(default_factory=list)
    reason_code: OutcomeReasonCode | None = None
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)
    created_at: datetime
```

## Artifacts Domain

### Session

The persisted metadata for one subagent run or resumable engine conversation.

- Preferred: `session`
- Python type: `Session`

```python
class Session(BaseModel):
    id: str
    task_id: str
    subagent_id: str
    engine: EngineName
    started_at: datetime
    completed_at: datetime | None = None
```

### Execution Trace

A human-readable render of what a subagent did or said during one run.

- Preferred: `execution trace`
- Python type: `ExecutionTrace`

```python
class ExecutionTrace(BaseModel):
    text: str
```

### Event Stream

The structured sequence of per-run events emitted by an engine or adapter.

- Preferred: `event stream`
- Python type: `EventStream`

```python
class EventStream(BaseModel):
    events: list[dict[str, object]] = Field(default_factory=list)
```

### Journal Entry

One machine-generated pipeline journal row.

- Preferred: `journal entry`
- Python type: `JournalEntry`

```python
class JournalEntry(BaseModel):
    seq: int
    created_at: datetime
    pipeline_state: PipelineState
    event_type: str
    payload: dict[str, object] = Field(default_factory=dict)
```

### Lifecycle Journal

The append-only machine-generated history of pipeline states and lifecycle
events.

- Preferred: `lifecycle journal`
- Python type: `LifecycleJournal`

```python
class LifecycleJournal(BaseModel):
    entries: list[JournalEntry] = Field(default_factory=list)
```

### Log Artifact

Raw operational output intended mainly for debugging or operator inspection.

- Preferred: `log artifact`
- Python type: `LogArtifact`

```python
class LogArtifact(BaseModel):
    kind: str
    path: Path
```

## Configuration Domain

### Litehive Config

Operator-controlled configuration for the workspace.

- Preferred: `litehive config`
- Python type: `LitehiveConfig`

```python
class LitehiveConfig(BaseModel):
    recovery_engine: EngineName | None = None
```
