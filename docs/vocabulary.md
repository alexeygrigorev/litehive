# Vocabulary

This document defines the canonical product and codebase vocabulary for
Litehive.

It is normative:

- use one canonical term per concept
- avoid historical labels when a clearer current term exists
- do not use `v2` in user-facing language
- prefer explicit owner prefixes such as `task status` or `runner status`
  instead of bare overloaded words

Some current file names and APIs still use older names. This document describes
the target vocabulary we want the codebase to converge on.

Where useful, entries show both:

- `Target Python type`: the intended long-term name
- `Current code shape`: the closest current class, field, or type alias in the
  existing codebase

## Naming Rules

- Use `pipeline` for the task execution flow and its persisted machine states.
- Use `lifecycle` for the internal mechanics that implement that flow:
  nodes, events, guards, and transitions.
- Use `task stage` for user-facing major work steps.
- Use `pipeline state` for the full internal machine state.
- Use `message` for human-readable text.
- Use `reason code` for normalized machine-readable classification.
- Use `rationale` for operator or agent explanation of a choice.
- Do not use bare `status`, `state`, `reason`, or `outcome` when more than one
  kind is in scope.

## Core Objects

### Workspace

The repository root managed by Litehive.

- Preferred: `workspace`
- Avoid: `repo` when the meaning is specifically the Litehive-managed working
  area rather than git in general

Target Python type:

- `Workspace`

Current code shape:

```python
# There is no dedicated Workspace class today.
# The repo root is usually passed around as Path, and runtime
# workspace state lives in WorkspaceState.
class WorkspaceState(BaseModel):
    active_task_id: str | None = None
    queue: list[str] = Field(default_factory=list)
    pool_stop_reason: str | None = None
```

### Task

A unit of work tracked by Litehive.

- Preferred: `task`

Target Python type:

- `Task`

Current code shape:

```python
class TaskRecord(BaseModel):
    id: str
    slug: str
    title: str
    status: TaskStatus
    pipeline_status: PipelineStatus
    runtime: TaskRuntime
```

### Task Runtime State

The mutable execution state attached to a task while Litehive runs.

- Preferred: `task runtime state`
- Short form when the context is obvious: `task runtime`
- This is not the same thing as `pipeline state`

Target Python type:

- `TaskRuntimeState`

Current code shape:

```python
class TaskRuntime(BaseModel):
    current_stage: RuntimeStageState = Field(default_factory=RuntimeStageState)
    active_subagent: RuntimeSubagentState | None = None
    interruption: RuntimeInterruptionState | None = None
    continuation_handoff: RuntimeContinuationHandoff | None = None
    last_outcome: TaskOutcomeState = Field(default_factory=TaskOutcomeState)
```

Typical contents include:

- current stage execution state
- retries and recovery bookkeeping
- active subagent
- interruption state
- continuation handoff

### Queue

The ordered list of tasks waiting to run.

- Preferred: `queue`
- Use `active task` for the currently running task
- Use `queued task` for a task waiting in the queue

Target Python type:

- `TaskQueue`

Current code shape:

```python
# The queue is not a dedicated class today.
# It is stored as a list of task ids on WorkspaceState.
class WorkspaceState(BaseModel):
    queue: list[str] = Field(default_factory=list)
```

### Engine

An external agent backend or integration that Litehive can use.

- Preferred: `engine`
- Examples:
  - `codex`
  - `claude`
  - `gemini`
- An engine is a capability provider, not one concrete run

Target Python type:

- `EngineName(str, Enum)` if engine names are modeled as a typed enum

Current code shape:

```python
# Engine names are still plain strings in most of the codebase.
engine_name: str
```

### Subagent

One task-scoped external agent execution launched by Litehive.

- Preferred: `subagent`
- Use `subagent run` when emphasizing one concrete execution attempt
- A subagent uses an engine
- One engine can power many subagent runs

Target Python type:

- `SubagentExecutionState`

Current code shape:

```python
class RuntimeSubagentState(BaseModel):
    id: str
    role: str
    engine: str
    status: SubagentStatus
    path: str
    continuation: RuntimeEngineContinuation | None = None
```

## Pipeline Model

### Task Stage

A user-facing major step in task execution.

- Preferred: `task stage`
- Short form when the context is obvious: `stage`

Target Python type:

- `TaskStage(str, Enum)`

Current code shape:

```python
# Task stages are still stored as string literals today.
StageReport.step: Literal[
    "grooming",
    "implementing",
    "testing",
    "accepting",
    "commit_to_git",
]
```

Canonical values:

- `grooming`: clarify scope, plan, and constraints
- `implementing`: make the code or content changes
- `testing`: verify the implementation with tests or checks
- `accepting`: review the work against the task goal and acceptance criteria
- `commit_to_git`: checkpoint accepted work into git
- `recovering`: run bounded diagnosis and repair after execution got stuck

### Stage Phase

The position of a pipeline state relative to a task stage.

- Preferred: `stage phase`
- Avoid using `phase` as a synonym for `pipeline state`

Target Python type:

- `StagePhase(str, Enum)`

Current code shape:

```python
# Stage phases are derived by naming convention today.
before("implementing") == "before_implementing"
after("implementing") == "after_implementing"
```

Canonical values:

- `before`: pre-stage setup and hooks
- `active`: the stage itself is executing
- `after`: post-stage hooks or finalization

### Pipeline State

The full internal machine state for task execution.

- Preferred: `pipeline state`
- This is the canonical name for the state-machine state
- Avoid calling this just `stage`

Target Python type:

- `PipelineState(str, Enum)`

Current code shape:

```python
# Pipeline states are still plain strings today.
NodeName = str
READY: NodeName = "ready"
RECOVERING: NodeName = "recovering"
TERMINAL_NODES = frozenset({"done", "failed"})
```

Canonical values:

- `ready`: task can be admitted into the pipeline
- `worktree_sync`: prepare or reconcile the task worktree
- `before_grooming`: pre-stage hook state for `grooming`
- `grooming`: active `grooming` stage
- `after_grooming`: post-stage hook state for `grooming`
- `before_implementing`: pre-stage hook state for `implementing`
- `implementing`: active `implementing` stage
- `after_implementing`: post-stage hook state for `implementing`
- `before_testing`: pre-stage hook state for `testing`
- `testing`: active `testing` stage
- `after_testing`: post-stage hook state for `testing`
- `before_accepting`: pre-stage hook state for `accepting`
- `accepting`: active `accepting` stage
- `after_accepting`: post-stage hook state for `accepting`
- `before_commit`: pre-stage hook state for `commit_to_git`
- `commit`: active commit stage
- `after_commit`: post-stage hook state for commit finalization
- `merge_resolving`: explicit merge-conflict resolution stage
- `recovering_pre_exec`: repair before entering the main pipeline
- `recovering`: bounded recovery after a pipeline failure or block
- `done`: terminal success state
- `failed`: terminal failure state

### Lifecycle Node

The executable object that owns behavior for a pipeline state.

- Preferred: `lifecycle node`
- Short form when the context is obvious: `node`
- Avoid using `node` as a synonym for `task stage`

Target Python type:

- `LifecycleNode`

Current code shape:

```python
# Nodes are concrete classes under litehive/lifecycle/nodes/.
class AgentNode:
    def run(self, state: TaskState, task: TaskRecord) -> Event: ...
```

### Lifecycle Event

A typed signal emitted by a lifecycle node or runner and consumed by the
transition rules.

- Preferred: `lifecycle event`
- Short form when the context is obvious: `event`
- Avoid `runtime event`

Target Python type:

- base class: `LifecycleEvent`

Current code shape:

```python
@dataclass(frozen=True)
class Event:
    pass


@dataclass(frozen=True)
class Reject(Event):
    source: Literal["agent", "hook", "guard", "system"]
    reason: str
```

Typical subclasses:

- `PassEvent`
- `RejectEvent`
- `BlockedEvent`
- `CrashEvent`
- `TimeoutEvent`
- `RecoverySucceededEvent`
- `RecoveryFailedEvent`

### Recovery Trigger

The structured input that explains why Litehive entered recovery.

- Preferred: `recovery trigger`

Target Python type:

- `RecoveryTrigger`

Current code shape:

```python
# Recovery trigger data is currently split across lifecycle state fields.
origin_stage: str | None
failure_context: dict[str, Any]
```

Typical fields:

- `origin_stage: TaskStage`
- `trigger_event_kind: TriggerEventKind`
- `message`
- optional diagnostics such as conflict files or fingerprints

### Trigger Event Kind

The normalized persisted label for the event that triggered recovery.

- Preferred field name: `trigger_event_kind`
- Avoid bare `trigger`

Target Python type:

- `TriggerEventKind(str, Enum)`

Current code shape:

```python
# Trigger kinds are still inferred from event classes or stored as strings.
type(event).__name__  # "Crash", "Blocked", "Reject", ...
```

Canonical values:

- `crash`: execution raised an unrecoverable error
- `timeout`: execution exceeded its time budget
- `blocked`: an agent cannot continue without external input
- `reject`: a stage result was not accepted
- `retry_limit`: a retry budget was exhausted
- `stage_retry_limit`: a per-stage retry budget was exhausted
- `pre_exec_recovery_failed`: pre-execution repair could not recover the task
- `merge_conflict`: commit or merge reconciliation needs intervention

## Statuses and Outcomes

### Task Status

The high-level execution or terminal category for a task.

- Preferred: `task status`

Target Python type:

- `TaskStatus(str, Enum)`

Current code shape:

```python
TaskStatus = Literal[
    "queued",
    "in_progress",
    "interrupted",
    "parked",
    "done",
    "flagged",
    "merge_failed",
    "cancelled",
    "wont_do",
    "deferred",
    "duplicate",
]
```

Canonical values:

- `queued`: waiting in the queue
- `in_progress`: currently being executed
- `interrupted`: execution stopped and can potentially resume
- `parked`: intentionally paused outside normal execution
- `done`: completed successfully
- `flagged`: requires explicit operator attention
- `merge_failed`: git integration ended in unresolved merge failure
- `cancelled`: explicitly cancelled
- `wont_do`: explicitly closed as not worth doing
- `deferred`: explicitly postponed
- `duplicate`: explicitly closed as duplicate work

### Pipeline Status

The task-facing projection of pipeline progress stored on the task record.

- Preferred: `pipeline status`
- This is a projection for task display and filtering
- It is not the same thing as the full `pipeline state`

Target Python type:

- `PipelineStatus(str, Enum)`

Current code shape:

```python
PipelineStatus = Literal[
    "backlog",
    "grooming",
    "implementing",
    "testing",
    "accepting",
    "commit_to_git",
    "done",
    "merge_failed",
    "flagged",
]
```

Canonical values:

- `backlog`: not yet admitted into execution
- `grooming`: in or around the grooming stage
- `implementing`: in or around the implementing stage
- `testing`: in or around the testing stage
- `accepting`: in or around the accepting stage
- `commit_to_git`: in or around the commit stage
- `done`: pipeline completed successfully
- `merge_failed`: pipeline stopped on merge failure
- `flagged`: pipeline stopped for operator attention

### Runner Status

The execution status of the top-level Litehive runner process.

- Preferred: `runner status`

Target Python type:

- `RunnerStatus(str, Enum)`

Current code shape:

```python
RunnerExecutionStatus = Literal["idle", "running", "late", "stale"]
```

Canonical values:

- `idle`: no active run
- `running`: actively executing work
- `late`: heartbeat is delayed
- `stale`: runner appears abandoned or dead

### Stage Run Status

The runtime status of one task stage attempt.

- Preferred: `stage run status`

Target Python type:

- `StageRunStatus(str, Enum)`

Current code shape:

```python
class RuntimeStageState(BaseModel):
    step: str | None = None
    status: str = "idle"
    verdict: str | None = None
```

Canonical values:

- `idle`: not currently running
- `running`: active now
- `interrupted`: stopped before a clean finish
- `completed`: ended cleanly

### Subagent Status

The runtime status of one subagent execution.

- Preferred: `subagent status`

Target Python type:

- `SubagentStatus(str, Enum)`

Current code shape:

```python
SubagentStatus = Literal[
    "queued", "running", "completed", "failed", "interrupted", "cancelled"
]
```

Canonical values:

- `queued`: allocated but not started
- `running`: active now
- `completed`: finished successfully
- `failed`: finished unsuccessfully
- `interrupted`: stopped before a clean finish
- `cancelled`: explicitly cancelled

### Task Close Outcome

The explicit operator-chosen terminal disposition when closing a task.

- Preferred: `task close outcome`
- Preferred field name: `close_outcome`

Target Python type:

- `TaskCloseOutcome(str, Enum)`

Current code shape:

```python
# Task close outcomes are currently passed as strings.
close_task(..., outcome="deferred")
```

Canonical values:

- `wont_do`: intentionally abandoned
- `deferred`: postponed for later
- `duplicate`: superseded by another task

### Recovery Disposition

The result category of one recovery attempt.

- Preferred: `recovery disposition`

Target Python type:

- `RecoveryDisposition(str, Enum)`

Current code shape:

```python
# Recovery results are currently represented by event classes.
RecoverySucceeded(resume="implementing")
RecoveryFailed(reason="...")
RecoveryBudgetHit()
```

Canonical values:

- `resumed`: return to the same task stage
- `advanced`: continue at a later task stage or pipeline state
- `done`: recovery concluded the task is complete
- `failed`: recovery could not restore a runnable path
- `budget_exceeded`: recovery was not allowed another attempt

### Stage Verdict

The decision submitted for a task stage.

- Preferred: `stage verdict`
- Short form when the context is obvious: `verdict`

Target Python type:

- `StageVerdict(str, Enum)`

Current code shape:

```python
TaskThreadComment.verdict: Literal["pass", "reject", "blocked", "comment"]
StageReport.verdict: Literal["pass", "accept", "fail", "reject", "blocked"]
```

Canonical values:

- `accept`: the stage goal is satisfied
- `reject`: the work is not acceptable, but Litehive can continue autonomously
- `blocked`: progress requires external input or operator decision

Why `blocked` is separate from `reject`:

- `reject` means the stage should loop or route normally because the problem is
  still within Litehive's execution scope
- `blocked` means the task cannot proceed without something outside that scope

`comment` is not a verdict in the target vocabulary. It is an activity entry
type or a free-form message, not a stage outcome.

### Message

Human-readable explanatory text.

- Preferred field name: `message`

Target Python type:

- `str`

Python sketch:

```python
message: str
```

### Reason Code

Normalized machine-readable classification.

- Preferred: `reason code`
- Preferred field name: `reason_code`

Target Python types:

- `OutcomeReasonCode(str, Enum)`
- `FailureReasonCode(str, Enum)` when a dedicated failure-code namespace helps

Current code shape:

```python
OutcomeReasonCode = Literal["verdict_reject", "execution_interrupted", ...]
FailedReason = Literal[
    "recovery_exhausted",
    "recovery_budget_hit",
    "recovery_crashed",
    "pre_exec_recovery_failed",
    "operator_abandoned",
    "unrecoverable_error",
]
```

### Rationale

Human explanation of why an operator or agent chose an action.

- Preferred field name: `rationale`

Target Python type:

- `str`

Python sketch:

```python
rationale: str
```

### Failure Fingerprint

A stable normalized identifier used to decide whether two failures are
materially the same.

- Preferred: `failure fingerprint`

Target Python type:

- `FailureFingerprint`

Current code shape:

```python
class RuntimeHookRejectFingerprint(BaseModel):
    point: str
    command: str
    description: str = ""
    fingerprint: str
```

Use it for:

- retry deduplication
- recovery-budget decisions
- grouping repeated failures in status or diagnostics

## Task-Attached History and Reports

### Task Activity

The append-only history attached to a task.

- Preferred: `task activity`
- This replaces vaguer terms like `discussion thread`

Target Python type:

- `TaskActivity`

Current code shape:

```python
# Task-attached history is currently stored as a list of TaskThreadComment.
comments: list[TaskThreadComment]
```

Typical contents include:

- agent notes
- operator notes
- verdict submissions
- system bookkeeping entries

### Activity Entry

One item in task activity.

- Preferred: `activity entry`
- Acceptable legacy term when discussing current storage only: `comment`

Target Python type:

- `ActivityEntry`

Current code shape:

```python
class TaskThreadComment(BaseModel):
    role: str
    step: str
    verdict: Literal["pass", "reject", "blocked", "comment"] = "comment"
    message: str
```

### Verdict Entry

An activity entry that carries a stage verdict.

- Preferred: `verdict entry`

Target Python type:

- `VerdictEntry`

Current code shape:

```python
# Any TaskThreadComment with verdict != "comment" acts as a verdict entry today.
comment.verdict in {"pass", "reject", "blocked"}
```

### Stage Report

A structured summary of what happened in one task stage run.

- Preferred: `stage report`

Target Python type:

- `StageReport`

Current code shape:

```python
class StageReport(BaseModel):
    task_id: str
    step: Literal["grooming", "implementing", "testing", "accepting", "commit_to_git"]
    verdict: Literal["pass", "accept", "fail", "reject", "blocked"]
    summary: str
    feedback: str = ""
```

Typical contents include:

- `task_stage`
- `stage_verdict`
- `summary`
- `message`
- changed files, tests, warnings, and diagnostics

### Recovery Outcome

The persisted result of one recovery attempt.

- Preferred: `recovery outcome`

Target Python type:

- `RecoveryOutcome`

Current code shape:

```python
class RecoveryReport(BaseModel):
    task_id: str
    stage: str | None = None
    trigger: str
    summary: str
    warnings: list[str] = Field(default_factory=list)
```

Typical contents include:

- `trigger: RecoveryTrigger`
- `disposition: RecoveryDisposition`
- `summary`
- `message`
- actions taken
- evidence gathered
- optional failure fingerprint and diagnostics

### Recovery Context

The structured recovery-entry payload carried forward into prompts and runtime
state.

- Preferred: `recovery context`
- Avoid the vague name `failure context`

Target Python type:

- `RecoveryContext`

Current code shape:

```python
failure_context: dict[str, Any]
origin_stage: str | None
```

## Execution Artifacts

### Session

The persisted metadata for one subagent run or one resumable engine
conversation.

- Preferred: `session`

Target Python type:

- `Session`

Current code shape:

```python
# Lifecycle session data lives in litehive/lifecycle/sessions.py and
# runtime subagent/session references are stored on RuntimeSubagentState.
session_id: str | None
```

### Execution Trace

A human-readable render of what a subagent did or said during one run.

- Preferred: `execution trace`
- Avoid `transcript` in new naming unless we intentionally keep the older word

Target Python type:

- `ExecutionTrace`

Current code shape:

```python
transcript_snippet: str
```

### Event Stream

The structured sequence of per-run events emitted by an engine or adapter.

- Preferred: `event stream`
- Avoid vague terms like `timeline` unless the UI is literally presenting a
  timeline view

Target Python type:

- `EventStream`

Current code shape:

```python
# Unified execution events currently live in engine-specific or Heru-owned
# event streams rather than one Litehive EventStream class.
events: list[dict[str, object]]
```

### Continuation

The engine-specific resume handle that allows a later run to continue an
existing session.

- Preferred: `continuation`

Target Python type:

- `ContinuationHandle`

Current code shape:

```python
RuntimeEngineContinuation
```

### Continuation Handoff

The durable record passed to a later run so it can resume execution.

- Preferred: `continuation handoff`

Target Python type:

- `ContinuationHandoff`

Current code shape:

```python
class RuntimeContinuationHandoff(BaseModel):
    step: str
    kind: Literal["retry", "engine_switch", "restart"]
    continuation: RuntimeEngineContinuation | None = None
```

### Lifecycle Journal

The append-only machine-generated history of pipeline states and lifecycle
events.

- Preferred: `lifecycle journal`
- Use `journal entry` for one row in that history
- Do not use `journal` as a synonym for task activity

Target Python types:

- `LifecycleJournal`
- `JournalEntry`

Current code shape:

```python
# Lifecycle journal rows are loaded from SQLite today.
row["created_at"]
row["kind"]
row["payload"]
```

### Log Artifact

Raw operational output intended mainly for debugging or operator inspection.

- Preferred: `log artifact`
- Use more specific names whenever possible:
  - `daemon log`
  - `stdout log`
  - `stderr log`

Do not use `log` as a synonym for:

- task activity
- lifecycle journal
- stage report
- execution trace

Target Python type:

- `LogArtifact`

Current code shape:

```python
stdout_path: Path
stderr_path: Path
```

## Storage Terms

### Configuration

Operator-controlled settings that define how Litehive should behave.

- Preferred: `configuration` or `config`
- Do not use `config` for mutable runtime state

Target Python type:

- `LitehiveConfig`

Current code shape:

```python
class LitehiveConfig(BaseModel):
    recovery_engine: str | None
```

### Runtime State

Mutable state produced while Litehive runs.

- Preferred: `runtime state`

Target Python type:

- `RuntimeState`

Current code shape:

```python
# Runtime state is currently split across TaskRuntime, TaskStateRecord,
# WorkspaceState, and lifecycle TaskState.
TaskRuntime
TaskStateRecord
WorkspaceState
```

### Durable Store

Structured persisted runtime storage.

- Preferred: `durable store` when speaking generally
- Use `SQLite` when the implementation detail matters

Target Python type:

- `WorkspaceRuntimeStore`

Current code shape:

```python
class RuntimeStore:
    def load_workspace_state(self) -> WorkspaceState | None: ...
    def load_task_state(self, task_id: str) -> TaskStateRecord | None: ...
    def save_task_state(self, task_id: str, state: TaskStateRecord) -> None: ...
```

### Repo-Local State

State stored under `.litehive/` inside the repository.

- Preferred: `repo-local state`

Target Python type:

- `RepoLocalState`

Current code shape:

```python
# Repo-local runtime data lives under .litehive/, not one RepoLocalState class.
Path(".litehive")
```

### Global Runtime Root

User-local runtime state stored under `${LITEHIVE_HOME}`.

- Preferred: `global runtime root`

Target Python type:

- `GlobalRuntimeRoot`

Current code shape:

```python
# Global runtime root is a filesystem location, not a dedicated class today.
Path("${LITEHIVE_HOME}")
```
