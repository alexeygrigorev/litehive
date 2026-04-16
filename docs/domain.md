# Domain Model

This document defines the desired domain model and canonical terminology for
Litehive.

It follows the general format defined in
[domain.spec.md](domain.spec.md).

It is normative:

- use one canonical term per concept
- do not use `v2` in user-facing language
- do not use bare overloaded words like `status`, `state`, `reason`, or
  `outcome` when more than one kind is in scope
- group related concepts by domain so review is local, not scattered

Entry-shape rules in this document:

- entities and records should say who creates them, why they exist, and who
  uses them after creation
- enums and classifiers should say who sets them and who uses them
- services and stores should justify why they exist as separate boundaries
- code sketches should show the target model, not mirror accidental current
  implementation details

## Contents

- [Modeling Terms](#modeling-terms)
- [Naming Rules](#naming-rules)
- [Cross-Domain Actors](#cross-domain-actors)
- [Workspace Domain](#workspace-domain)
- [Task Domain](#task-domain)
- [Pipeline Domain](#pipeline-domain)
- [Recovery Domain](#recovery-domain)
- [Execution Domain](#execution-domain)
- [Activity Domain](#activity-domain)
- [Artifacts Domain](#artifacts-domain)
- [Configuration Domain](#configuration-domain)

## Modeling Terms

This document uses lightweight Domain-Driven Design terminology so the domain
model can double as design and education material.

### Domain

A coherent area of the product model with its own concepts, rules, actors, and
actions.

Examples in Litehive:

- workspace
- task
- pipeline
- recovery
- execution
- activity
- artifacts
- configuration

### Ubiquitous Language

The shared set of words that code, docs, CLI help, and human discussion should
all use for the same concepts.

Rule:

- if the code says `PipelineState`, the docs and CLI should not casually call
  the same thing `phase`, `status`, `node`, or `step`

### Entity

An object with identity that persists over time even as its fields change.

Examples in Litehive:

- `Task`
- `SubagentRun`
- `Session`

Questions that usually indicate an entity:

- does it have a stable id?
- do we load and save it over time?
- do we care that this is the same object after updates?

### Value Object

A small descriptive object whose meaning comes from its fields, not from a
stable identity.

Examples in Litehive:

- `TaskRetryPolicy`
- `FailureDiagnostics`
- `FailureFingerprint`

Questions that usually indicate a value object:

- does it describe something rather than stand alone as its own thing?
- would replacing it with an equal value be fine?

### Aggregate

A consistency boundary around one main entity and the data that must change
together with it.

Working Litehive interpretation:

- `Task` is the main aggregate root
- `TaskRuntime`, `TaskActivity`, and related task-attached records exist to
  support the task lifecycle

Design rule:

- prefer loading and changing task-scoped data through task-oriented services
  instead of letting unrelated modules mutate pieces independently

### Service

An object that performs domain actions that do not naturally belong on one
entity or value object.

Examples in Litehive:

- `TaskService`
- `QueueService`
- `PipelineRunner`
- `RecoveryCoordinator`

Use a service when:

- the behavior coordinates multiple entities
- the behavior talks to stores, engines, or external systems
- putting the behavior on one model object would make that object misleading or
  too large

### Store

The persistence boundary responsible for loading and saving structured domain
objects.

This document uses `store` as the neutral term. Later code may choose more
specific names such as `repository` if that becomes useful.

### Actor

A person or system component that does work in the domain.

Examples in Litehive:

- `Operator`
- `Runner`
- `PipelineNode`
- `Subagent`

We name actors explicitly so each domain explains not only what data exists,
but who uses it and why.

### Action

A meaningful domain operation performed by an actor or service.

Examples in Litehive:

- enqueue a task
- flag a task
- apply a pipeline event
- start recovery
- append an activity entry

### Event

A typed fact that something already happened and may trigger follow-up work.

Examples in Litehive:

- `AcceptEvent`
- `RejectEvent`
- `BlockedEvent`
- `CrashEvent`

Rule:

- use `PipelineEvent` for runtime transition facts
- do not use vague names like `event` or `runtime event` when a more specific
  type exists

## Naming Rules

- Use `task` for the core work item.
- Use `pipeline` for the task execution flow.
- Use `message` for human-readable text.
- Use `reason_code` for normalized machine-readable classification.
- Use `rationale` for operator or agent explanation of a choice.

## Cross-Domain Actors

This section is global rather than domain-local because these actors operate
across multiple domains instead of belonging to only one of them.

### Operator

The human using Litehive through CLI commands and reviewing task state.

Exists because:

- Litehive is an operator-driven system and many decisions still come from a
  human user

Uses:

- tasks, queue state, activity, reports, recovery results, and configuration

### Runner

The top-level process that selects tasks, advances the pipeline, persists
runtime state, and reacts to pipeline events.

Exists because:

- task execution needs one top-level process that owns orchestration across
  domains

Uses:

- queue state, tasks, pipeline runtime, recovery, and artifacts

### Pipeline Node

One executable unit that owns the behavior of one pipeline state and emits a
typed pipeline event when it finishes.

Exists because:

- each pipeline state needs one isolated behavior unit instead of one giant
  execution function

Uses:

- tasks, pipeline state, prompts, engine execution, and pipeline events

### Subagent

One external engine-backed execution launched by Litehive for a specific role
such as planning, implementation, QA, review, or recovery.

Exists because:

- Litehive delegates stage work to external agent engines rather than doing the
  work inside the runner process

Uses:

- prompts, task context, engine adapters, and activity/report submission paths

### Store

The persistence boundary that loads and saves structured runtime data.

Exists because:

- persistence concerns should stay behind explicit boundaries instead of being
  spread through every service and command

Uses:

- domain records such as tasks, queue state, activity, and artifacts

## Workspace Domain

Purpose:
workspace-scoped coordination and persistence. This domain exists so Litehive
can answer simple operational questions such as "which task is active",
"what is queued next", and "where is task state stored".

Primary actors:

- `Operator`: inspects the queue and manually changes task ordering
- `Runner`: claims the next task and marks the active task
- `WorkspaceStore`: loads and saves queue and task data

Primary actions:

- enqueue a task
- remove a task from the queue
- mark the active task
- load and save task records

### Workspace

The repository root managed by Litehive.

Set by:

- the operator by choosing where Litehive runs

Used by:

- all stores, services, and CLI commands as the root location for task and
  runtime data

- Preferred: `workspace`
- Python type: `Path`

```python
workspace: Path
```

### Task Queue

The ordered list of tasks waiting to run in one workspace.

Created by:

- `QueueService` when initializing or updating queue state

Exists because:

- tracking which task is active and which tasks are waiting to run

Used after creation by:

- `Runner` to pick the next task
- `Operator` and `QueueService` to inspect and reorder work

- Preferred: `task queue`
- Python type: `TaskQueue`

```python
class TaskQueue(BaseModel):
    active_task_id: str | None = None
    queued_task_ids: list[str] = Field(default_factory=list)
```

### Workspace Store

The durable storage API for workspace runtime data.

Exists because:

- queue and task persistence should have one explicit storage boundary instead
  of leaking database access into every command and service

Used by:

- `QueueService`, `TaskService`, `PipelineRunner`, and CLI commands

- Preferred: `workspace store`
- Python type: `WorkspaceStore`

```python
class WorkspaceStore(ABC):
    @abstractmethod
    def load_task(self, task_id: str) -> Task | None: ...

    @abstractmethod
    def save_task(self, task: Task) -> None: ...

    @abstractmethod
    def load_queue(self) -> TaskQueue: ...

    @abstractmethod
    def save_queue(self, queue: TaskQueue) -> None: ...
```

### Queue Service

The service that applies queue operations using the workspace store.

Exists because:

- queue changes are workspace-level operations and should not be spread across
  unrelated commands

Used by:

- CLI commands and `Runner` when ordering or claiming tasks

- Preferred: `queue service`
- Python type: `QueueService`

```python
class QueueService:
    def enqueue(self, task_id: str) -> None: ...
    def dequeue(self, task_id: str) -> None: ...
    def promote(self, task_id: str) -> None: ...
    def set_active_task(self, task_id: str | None) -> None: ...
```

## Task Domain

Purpose:
the task is the main product object in Litehive. This domain exists to define
what a task is, which fields are operator intent versus runtime bookkeeping,
and which terminal or attention states a task can have.

Primary actors:

- `Operator`: creates, edits, reprioritizes, closes, and flags tasks
- `Planner` and other subagents: read task intent and write back activity
- `Runner`: updates task status as execution progresses
- `TaskService`: owns task-level mutations

Primary actions:

- create a task
- update task intent
- reprioritize a task
- flag a task
- close a task

### Task Status

The high-level execution or terminal category for a task.

Set by:

- `TaskService`, `PipelineRunner`, and operator-facing CLI commands

Used by:

- queueing, filtering, reporting, and operator decisions about what happens
  next

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
    CLOSED = "closed"
```

Values:

- `queued`: waiting in the queue
- `in_progress`: currently executing
- `interrupted`: execution stopped and can potentially resume
- `parked`: intentionally paused by Litehive or the operator
- `done`: completed successfully
- `flagged`: requires explicit operator attention
- `closed`: intentionally ended without successful completion

Why there is no separate `cancelled` status:

- `closed` is the state
- `close_reason` explains why it was closed

### Task Close Reason

The explicit operator-chosen reason when closing a task.

Set by:

- `TaskService` or CLI close actions

Used by:

- CLI views and reporting to explain why a closed task was ended

- Preferred: `task close reason`
- Preferred field name: `close_reason`
- Python type: `TaskCloseReason`

```python
class TaskCloseReason(str, Enum):
    CANCELLED = "cancelled"
    WONT_DO = "wont_do"
    DEFERRED = "deferred"
    DUPLICATE = "duplicate"
```

Values:

- `cancelled`: the operator intentionally stopped this task
- `wont_do`: the task is no longer worth doing
- `deferred`: the task should wait for later
- `duplicate`: another task already covers the same work

### Task Flag Reason

The normalized reason a task was flagged.

Set by:

- `PipelineRunner` and recovery flows when autonomous execution stops

Used by:

- operators and reporting to understand why manual attention is required

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

Flagging mechanism:

- `Runner` or `PipelineRunner` sets `status=FLAGGED` when autonomous progress
  must stop
- `flag_reason` records the machine-readable cause
- task activity should contain the human explanation

### Task Priority

The scheduling priority of a task.

Set by:

- the operator or task-management commands

Used by:

- queue ordering and task selection logic

- Preferred: `task priority`
- Python type: `TaskPriority`

```python
class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
```

Values:

- `critical`: highest scheduling urgency
- `high`: urgent work
- `medium`: default urgency
- `low`: lowest scheduling urgency

Scheduling order:

- when priorities are sorted for selection, use `critical`, `high`, `medium`,
  `low`
- lower numeric rank means higher priority

```python
TASK_PRIORITY_ORDER = {
    TaskPriority.CRITICAL: 0,
    TaskPriority.HIGH: 1,
    TaskPriority.MEDIUM: 2,
    TaskPriority.LOW: 3,
}
```

### Task Retry Policy

The configured retry limits for a task.

Set by:

- task creation and task-edit flows

Used by:

- `PipelineRunner` and recovery logic when deciding whether another attempt is
  allowed

- Preferred: `task retry policy`
- Python type: `TaskRetryPolicy`

```python
class TaskRetryPolicy(BaseModel):
    max_retries: int | None = None
    state_retry_limit: int | None = None
```

### Task

A unit of work tracked by Litehive.

Created by:

- `TaskService` when the operator creates a new task
- follow-up task creation flows when one task produces another task

Exists because:

- storing the operator's intended work item plus its execution-attached runtime
  state

Used after creation by:

- `PipelineRunner`, `RecoveryCoordinator`, and CLI commands to read and update
  task progress
- subagents and prompts to understand the task goal, plan, and constraints

- Preferred: `task`
- Python type: `Task`

```python
class Task(BaseModel):
    id: str
    slug: str
    title: str
    pipeline_mode: PipelineMode = PipelineMode.FULL
    model: str | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    status: TaskStatus = TaskStatus.QUEUED
    close_reason: TaskCloseReason | None = None
    flag_reason: TaskFlagReason | None = None
    created_at: datetime
    updated_at: datetime
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)
    runtime: TaskRuntime = Field(default_factory=TaskRuntime)
```

Field meanings:

- `goal`: the main intended result
- `acceptance_criteria`: concrete completion conditions
- `constraints`: limitations or rules that must be respected
- `plan`: the current working plan for the task
- `depends_on`: upstream task ids
- `runtime`: mutable execution-only state, separated from task intent

Why `default_factory` is used here:

- `TaskRuntime` is mutable state
- each `Task` must get its own fresh `TaskRuntime`
- `Field(default_factory=TaskRuntime)` creates a new instance per task
- using `runtime: TaskRuntime = TaskRuntime()` directly would risk sharing one
  mutable default across instances

### Task Service

The service that owns task-level mutations.

Exists because:

- task creation, closure, flagging, and plan updates are task-level operations
  that should be centralized

Used by:

- CLI commands, follow-up task creation, and task-management flows

- Preferred: `task service`
- Python type: `TaskService`

```python
class TaskService:
    def create(self, title: str, *, goal: str = "") -> Task: ...
    def update_plan(self, task_id: str, plan: list[str]) -> None: ...
    def reprioritize(self, task_id: str, priority: TaskPriority) -> None: ...
    def flag(self, task_id: str, reason: TaskFlagReason, message: str) -> None: ...
    def close(self, task_id: str, reason: TaskCloseReason, message: str) -> None: ...
```

## Pipeline Domain

Purpose:
the pipeline is the execution flow for a task. This domain exists so Litehive
can model where a task is, what the runner is doing right now, and which event
causes the next transition.

Primary actors:

- `Runner`: advances the task through pipeline states
- `PipelineNode`: performs the work for one pipeline state
- `TransitionRules`: map pipeline events to next states
- `PipelineRunner`: coordinates the whole execution step

Primary actions:

- start a task run
- execute one pipeline state
- emit a pipeline event
- apply a state transition
- retry or enter recovery

### Pipeline Mode

The top-level execution mode for a task.

Set by:

- task creation or operator task-edit commands

Used by:

- `PipelineRunner` when deciding which states are eligible for the task

- Preferred: `pipeline mode`
- Python type: `PipelineMode`

```python
class PipelineMode(str, Enum):
    FULL = "full"
    SINGLE = "single"
```

Values:

- `full`: run the full pipeline from grooming through commit
- `single`: skip early planning states and start directly in implementation

### Pipeline State

The full internal machine state for task execution.

Set by:

- `PipelineRunner` through transition rules

Used by:

- pipeline routing, prompts, journaling, recovery, and status displays

- Preferred: `pipeline state`
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

State meanings:

- `ready`: task is admitted and about to start
- `worktree_sync`: workspace preparation and reconciliation
- `before_*`: pre-state hooks
- active states such as `implementing` or `testing`: the main work states
- `after_*`: post-state hooks
- `merge_resolving`: explicit merge-conflict resolution
- `recovering_pre_exec`: cleanup before the main pipeline can safely start
- `recovering`: bounded recovery after a failure or block
- `done`: terminal success
- `failed`: terminal failure

Why `PipelineState` is enough:

- it already expresses both the coarse stage and the exact machine state
- a separate `pipeline state view` would duplicate information

### Pipeline Run Status

The execution status of the current pipeline run.

Set by:

- `PipelineRunner`

Used by:

- resume logic, CLI status displays, and recovery decisions

- Preferred: `pipeline run status`
- Python type: `PipelineRunStatus`

```python
class PipelineRunStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
```

Why this exists separately from `PipelineState`:

- `PipelineState` answers "where in the flow are we?"
- `PipelineRunStatus` answers "is that flow currently active, paused, or done?"

Example:

- `pipeline_state=IMPLEMENTING` and `run_status=RUNNING` means implementation
  is actively executing
- `pipeline_state=IMPLEMENTING` and `run_status=INTERRUPTED` means the task was
  interrupted while implementing and may resume later

### Stage Verdict

The decision submitted for an executable pipeline state.

Created by:

- subagents and hook execution paths when they submit the result of a pipeline
  state

Exists because:

- expressing the high-level outcome of one executable pipeline state in a small
  normalized form

Used after creation by:

- `PipelineRunner` to decide whether to advance, retry, block, or enter
  recovery
- `ActivityEntry`, `StageReport`, and `TaskOutcome` as the canonical decision
  value

- Preferred: `stage verdict`
- Python type: `StageVerdict`

```python
class StageVerdict(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    BLOCKED = "blocked"
```

Values:

- `accept`: the state goal was achieved
- `reject`: the result is not acceptable, but Litehive can still continue
- `blocked`: progress requires external operator input or a missing dependency

### Pipeline Run

The mutable record of the current or last executed pipeline state.

Created by:

- `PipelineRunner` when a task enters or updates a pipeline state

Exists because:

- tracking timing, verdict, and summary data for one active or recently
  completed pipeline state

Used after creation by:

- `PipelineRuntime` to expose current and last run state
- reporting and debugging views to show what happened in a pipeline state

- Preferred: `pipeline run`
- Python type: `PipelineRun`

```python
class PipelineRun(BaseModel):
    pipeline_state: PipelineState | None = None
    run_status: PipelineRunStatus = PipelineRunStatus.IDLE
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None
    duration_seconds: int = 0
    verdict: StageVerdict | None = None
    summary: str = ""
```

### Pipeline Event Source

The origin of a pipeline event.

Set by:

- the code path that creates a `PipelineEvent`

Used by:

- transition logic, debugging, and reporting to explain where a decision came
  from

- Preferred: `pipeline event source`
- Python type: `PipelineEventSource`

```python
class PipelineEventSource(str, Enum):
    AGENT = "agent"
    HOOK = "hook"
    GUARD = "guard"
    SYSTEM = "system"
```

### Pipeline Event

A typed signal emitted by a pipeline node or runner and consumed by transition
rules.

Created by:

- `PipelineNode` implementations
- `PipelineRunner` when the runner itself detects an interruption or timeout

Exists because:

- telling transition rules what just happened so the next pipeline state can be
  chosen

Used after creation by:

- `PipelineRunner` and transition rules to route execution to the next state
- journaling and debugging code to explain why a transition happened

- Preferred: `pipeline event`
- Python type: `PipelineEvent`

```python
@dataclass(frozen=True)
class PipelineEvent:
    source: PipelineEventSource
    message: str = ""


@dataclass(frozen=True)
class AcceptEvent(PipelineEvent):
    metadata: dict[str, str | int | bool | None] = field(default_factory=dict)


@dataclass(frozen=True)
class RejectEvent(PipelineEvent):
    metadata: dict[str, str | int | bool | None] = field(default_factory=dict)


@dataclass(frozen=True)
class BlockedEvent(PipelineEvent):
    pass


@dataclass(frozen=True)
class CrashEvent(PipelineEvent):
    exc_type: str = ""


@dataclass(frozen=True)
class TimeoutEvent(PipelineEvent):
    pass
```

Event meanings:

- `AcceptEvent`: continue on the happy path
- `RejectEvent`: the result is not acceptable, but the pipeline still owns the
  next decision
- `BlockedEvent`: an external decision or missing input is required
- `CrashEvent`: execution hit an unrecoverable error
- `TimeoutEvent`: execution exceeded its allowed time budget

### Pipeline Node

The executable object that owns behavior for one pipeline state.

Created by:

- pipeline setup code when building the runnable state machine

Exists because:

- isolating the behavior for one pipeline state behind one executable unit

Used after creation by:

- `PipelineRunner` when it executes the current state

- Preferred: `pipeline node`
- Python type: `PipelineNode`

```python
class PipelineNode(ABC):
    @abstractmethod
    def run(self, task: Task, pipeline_state: PipelineState) -> PipelineEvent: ...
```

### Pipeline Runner

The coordinator that executes pipeline nodes, manages subagent execution, and
applies transitions.

Exists because:

- pipeline progression, subagent orchestration, and runtime updates need one
  coordinating service

Used by:

- `Runner` as the main task-execution engine

- Preferred: `pipeline runner`
- Python type: `PipelineRunner`

```python
class PipelineRunner:
    def start(self, task_id: str) -> None: ...
    def run_next_state(self, task_id: str) -> None: ...
    def apply_event(self, task_id: str, event: PipelineEvent) -> None: ...
    def launch_subagent(self, task_id: str, role: ExecutionRole, engine: EngineName) -> SubagentRun: ...
    def interrupt_subagent(self, task_id: str, message: str) -> None: ...
    def resume_subagent(self, task_id: str) -> None: ...
    def switch_engine(self, task_id: str, to_engine: EngineName, rationale: str) -> None: ...
```

## Recovery Domain

Purpose:
recovery exists because a normal pipeline state can fail, block, or time out.
This domain defines what triggered recovery, what recovery concluded, and what
context should be available if recovery hands control back to the pipeline.

Primary actors:

- `Runner`: decides to enter recovery
- `Recovery subagent`: investigates and proposes how to continue
- `RecoveryCoordinator`: records the trigger and result
- `Operator`: intervenes when recovery fails

Primary actions:

- enter recovery
- persist the recovery trigger
- run one recovery attempt
- mark recovery success or failure
- resume the pipeline or flag the task

### Trigger Event Kind

The normalized label for the event that triggered recovery.

Set by:

- `PipelineRunner` when converting a pipeline failure into a recovery trigger

Used by:

- `RecoveryCoordinator`, prompts, and reporting to classify the recovery case

- Preferred field name: `trigger_event_kind`
- Python type: `TriggerEventKind`

```python
class TriggerEventKind(str, Enum):
    CRASH = "crash"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    REJECT = "reject"
    RETRY_LIMIT = "retry_limit"
    PRE_EXEC_RECOVERY_FAILED = "pre_exec_recovery_failed"
    MERGE_CONFLICT = "merge_conflict"
```

### Failure Fingerprint

A stable normalized identifier used to decide whether two failures are
materially the same.

Created by:

- failure-classification code when a repeatable failure signature can be
  derived

Exists because:

- detecting repeated or identical failures across attempts

Used after creation by:

- recovery and reporting logic when grouping failures

- Preferred: `failure fingerprint`
- Python type: `FailureFingerprint`

```python
class FailureFingerprint(BaseModel):
    kind: str
    value: str
```

### Failure Diagnostics

Structured diagnostics attached to a failure, whether it appears in a stage
report or a recovery trigger.

Created by:

- reporting, failure-classification, and recovery-preparation code

Exists because:

- failure classification should use one shared structured type instead of
  separate near-duplicate report and recovery models

Used after creation by:

- reporting, recovery, debugging, and retry analysis

- Preferred: `failure diagnostics`
- Python type: `FailureDiagnostics`

```python
class FailureDiagnostics(BaseModel):
    classification: str | None = None
    fingerprint: FailureFingerprint | None = None
    conflict_files: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
```

### Recovery Trigger

The structured input that explains why Litehive entered recovery.

Created by:

- `PipelineRunner` when a normal pipeline state routes into recovery

Exists because:

- recording why recovery started and what state/event caused it

Used after creation by:

- `RecoveryCoordinator` to persist recovery bookkeeping
- `PipelineRuntime` and recovery prompts to explain the active recovery context

- Preferred: `recovery trigger`
- Python type: `RecoveryTrigger`

```python
class RecoveryTrigger(BaseModel):
    origin_state: PipelineState
    trigger_event_kind: TriggerEventKind
    message: str = ""
    diagnostics: FailureDiagnostics | None = None
```

### Recovery Result

The result category of one recovery attempt.

Set by:

- `RecoveryCoordinator` when recovery finishes

Used by:

- `PipelineRunner` and reporting to decide whether recovery resumes or fails

- Preferred: `recovery result`
- Python type: `RecoveryResult`

```python
class RecoveryResult(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
```

Why this is intentionally small:

- the primary routing question is whether recovery restored a runnable path
- detailed evidence belongs in logs, activity, and artifacts

### Recovery Record

The persisted summary of one recovery attempt.

Created by:

- `RecoveryCoordinator` when a recovery attempt finishes

Exists because:

- recording whether recovery succeeded and where control should go next

Used after creation by:

- `PipelineRunner` and CLI/reporting code to show recovery history
- later debugging and analytics to understand recovery effectiveness

- Preferred: `recovery record`
- Python type: `RecoveryRecord`

```python
class RecoveryRecord(BaseModel):
    task_id: str
    trigger: RecoveryTrigger
    result: RecoveryResult
    next_state: PipelineState | None = None
    summary: str = ""
    created_at: datetime
```

### Recovery Context

The recovery-entry payload carried into runtime state and prompts.

Created by:

- `PipelineRunner` or `RecoveryCoordinator` when entering recovery

Exists because:

- giving recovery prompts and runtime state immediate access to the trigger and
  latest relevant rejection context

Used after creation by:

- `PipelineRuntime` while the task is in recovery
- recovery prompts and resume logic to understand the current recovery case

- Preferred: `recovery context`
- Python type: `RecoveryContext`

```python
class RecoveryContext(BaseModel):
    trigger: RecoveryTrigger
    last_rejection: ActivityEntry | None = None
```

### Recovery Coordinator

The service that owns recovery bookkeeping and recovery policy.

Exists because:

- recovery has its own policy and persistence rules separate from normal
  pipeline progression

Used by:

- `PipelineRunner` when entering or finishing recovery

- Preferred: `recovery coordinator`
- Python type: `RecoveryCoordinator`

```python
class RecoveryCoordinator:
    def start_recovery(self, task_id: str, trigger: RecoveryTrigger) -> None: ...
    def finish_recovery(
        self,
        task_id: str,
        *,
        result: RecoveryResult,
        next_state: PipelineState | None,
        summary: str,
    ) -> None: ...
```

## Execution Domain

Purpose:
this domain holds live execution data. It exists so Litehive can manage
subagent processes, resumable sessions, interruptions, and the mutable runtime
state attached to an executing task.

Primary actors:

- `Runner`: owns the top-level task execution loop
- `PipelineRunner`: launches, resumes, and interrupts subagents
- `EngineAdapter`: provides engine-specific execution behavior
- `Subagent`: performs one role-specific run

Primary actions:

- launch a subagent
- interrupt a run
- resume a session
- switch engines
- persist runtime state

### Runner Status

The execution status of the top-level Litehive runner process.

Set by:

- the top-level runner process

Used by:

- monitoring, daemon/CLI status views, and stale-run detection

- Preferred: `runner status`
- Python type: `RunnerStatus`

```python
class RunnerStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    LATE = "late"
    STALE = "stale"
```

### Engine Name

The normalized execution engine identifier.

Set by:

- config, task settings, and engine-selection logic

Used by:

- engine adapters, prompts, execution orchestration, and reporting

- Preferred: `engine name`
- Python type: `EngineName`

```python
class EngineName(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    GEMINI = "gemini"
    COPILOT = "copilot"
    OPENCODE = "opencode"
    GOZ = "goz"
```

### Subagent Status

The runtime status of one subagent execution.

Set by:

- `PipelineRunner` and engine-execution logic

Used by:

- runtime views, interruption handling, resume logic, and reporting

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

Set by:

- `PipelineRunner` when launching a subagent

Used by:

- prompt construction, activity attribution, and reporting

- Preferred: `execution role`
- Python type: `ExecutionRole`

```python
class ExecutionRole(str, Enum):
    PLANNER = "planner"
    SWE = "swe"
    QA = "qa"
    REVIEWER = "reviewer"
    RECOVERY = "recovery"
    MERGE_RESOLVER = "merge_resolver"
```

### Subagent Run

The runtime record of one subagent execution.

Created by:

- `PipelineRunner` when it launches a subagent for a pipeline state

Exists because:

- tracking one engine-backed run with identity, status, timing, and resume data

Used after creation by:

- `ExecutionRuntime` to expose active and last subagent state
- artifact-writing and reporting code to associate traces, logs, and outcomes
  with one run

- Preferred: `subagent run`
- Python type: `SubagentRun`

```python
class SubagentRun(BaseModel):
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
    session_token: str | None = None
```

Fields intentionally not kept here:

- log excerpts and trace snippets belong in the artifacts domain
- interruption explanations belong in `InterruptionRecord`
- sandbox details belong in execution artifacts or structured diagnostics

### Interruption Source

The source that initiated an interruption.

Set by:

- the code that creates an `InterruptionRecord`

Used by:

- resume and debugging flows to tell whether the runner or the subagent caused
  the interruption

- Preferred: `interruption source`
- Python type: `InterruptionSource`

```python
class InterruptionSource(str, Enum):
    RUNNER = "runner"
    SUBAGENT = "subagent"
```

### Interruption Record

The runtime record describing an interruption.

Created by:

- `PipelineRunner` when execution is interrupted by the runner or a subagent

Exists because:

- preserving why execution stopped and which active run was affected

Used after creation by:

- `ExecutionRuntime` while a task is interrupted
- resume and recovery logic to decide how execution should continue

- Preferred: `interruption record`
- Python type: `InterruptionRecord`

```python
class InterruptionRecord(BaseModel):
    source: InterruptionSource
    pipeline_state: PipelineState | None = None
    message: str = ""
    interrupted_at: datetime | None = None
    subagent_id: str | None = None
```

### Engine Switch

The record of switching execution from one engine to another.

Created by:

- `PipelineRunner` when retry or recovery logic moves execution to a different
  engine

Exists because:

- recording the previous engine, next engine, and rationale for the switch

Used after creation by:

- `ExecutionRuntime` and debugging views to explain why execution changed engines
- retry and recovery analysis to understand engine-switch behavior

- Preferred: `engine switch`
- Python type: `EngineSwitch`

```python
class EngineSwitch(BaseModel):
    pipeline_state: PipelineState
    from_engine: EngineName
    to_engine: EngineName
    rationale: str
    happened_at: datetime
```

### Continuation Kind

The reason a later run can continue from earlier execution state.

Set by:

- `PipelineRunner` when creating a `ContinuationHandoff`

Used by:

- resume logic and prompts to understand whether this is a retry, restart, or
  engine switch

- Preferred: `continuation kind`
- Python type: `ContinuationKind`

```python
class ContinuationKind(str, Enum):
    RETRY = "retry"
    ENGINE_SWITCH = "engine_switch"
    RESTART = "restart"
```

### Continuation Handoff

The durable record passed to a later run so it can resume execution.

Created by:

- `PipelineRunner` when a run ends in a resumable state

Exists because:

- telling the next run how to resume after retry, restart, or engine switch

Used after creation by:

- `PipelineRunner` when launching the next run
- prompts and runtime state so the next execution attempt knows what to resume

- Preferred: `continuation handoff`
- Python type: `ContinuationHandoff`

```python
class ContinuationHandoff(BaseModel):
    pipeline_state: PipelineState
    kind: ContinuationKind
    from_engine: EngineName | None = None
    to_engine: EngineName | None = None
    session_token: str | None = None
    summary: str = ""
    updated_at: datetime
```

Note:

- `session_token` is mainly useful when resuming on the same engine
- after an engine switch it may be empty because the new engine cannot reuse
  the old engine's continuation data

### Task Outcome

The latest recorded execution outcome for a task.

Created by:

- `PipelineRunner` when a pipeline state finishes, interrupts, or fails

Exists because:

- exposing one normalized summary of the latest task execution result

Used after creation by:

- queueing, retry, and recovery logic to make routing decisions
- reporting and CLI views to summarize task execution state

- Preferred: `task outcome`
- Python type: `TaskOutcome`

```python
class TaskOutcome(BaseModel):
    pipeline_state: PipelineState | None = None
    reason_code: OutcomeReasonCode | None = None
    message: str = ""
    retry_count: int = 0
    retry_limit: int = 0
    recorded_at: datetime | None = None
```

### Pipeline Runtime

The pipeline-specific mutable state attached to a task while it runs.

Created by:

- `TaskService` when a new task is created
- `PipelineRunner` as pipeline state changes are recorded

Exists because:

- pipeline position, retries, current run details, and recovery context belong
  together and are easier to understand as one slice

Used after creation by:

- `PipelineRunner`, recovery logic, and CLI status views

- Preferred: `pipeline runtime`
- Python type: `PipelineRuntime`

```python
class PipelineRuntime(BaseModel):
    state: PipelineState = PipelineState.READY
    run_status: PipelineRunStatus = PipelineRunStatus.IDLE
    run_started_at: datetime | None = None
    updated_at: datetime | None = None
    retry_count: int = 0
    retry_limit: int = 0
    state_retry_counts: dict[PipelineState, int] = Field(default_factory=dict)
    current_run: PipelineRun = Field(default_factory=PipelineRun)
    last_run: PipelineRun = Field(default_factory=PipelineRun)
    recovery_context: RecoveryContext | None = None
    last_outcome: TaskOutcome = Field(default_factory=TaskOutcome)
```

### Execution Runtime

The execution-specific mutable state for subagent runs and resumability.

Created by:

- `TaskService` when a new task is created
- `PipelineRunner` as subagent execution proceeds

Exists because:

- subagent state, interruption handling, continuation, and engine switching are
  a separate concern from pipeline position and retries

Used after creation by:

- `PipelineRunner`, resume logic, and debugging views

- Preferred: `execution runtime`
- Python type: `ExecutionRuntime`

```python
class ExecutionRuntime(BaseModel):
    active_subagent: SubagentRun | None = None
    last_subagent: SubagentRun | None = None
    interruption: InterruptionRecord | None = None
    continuation_handoff: ContinuationHandoff | None = None
    last_engine_switch: EngineSwitch | None = None
```

### Task Runtime

The task-scoped container for mutable runtime state.

Created by:

- `TaskService` when a new task is created

Exists because:

- the task still needs one runtime field, but the internals are easier to
  understand when split into pipeline and execution slices

Used after creation by:

- `PipelineRunner`, CLI, reporting, and recovery code

- Preferred: `task runtime`
- Python type: `TaskRuntime`

```python
class TaskRuntime(BaseModel):
    pipeline: PipelineRuntime = Field(default_factory=PipelineRuntime)
    execution: ExecutionRuntime = Field(default_factory=ExecutionRuntime)
```

Execution orchestration note:

- this target model intentionally keeps subagent launch, interruption, resume,
  and engine switching inside `PipelineRunner`
- that gives Litehive one orchestration service instead of splitting execution
  control across two services
- engine-specific behavior still belongs in engine adapters, not in task
  models or reports

## Activity Domain

Purpose:
this domain exists for human-readable task-attached history. It is where
operators and agents explain what happened, submit verdicts, and leave
structured summaries that are easier to review than raw logs.

Primary actors:

- `Operator`: leaves notes and closes or flags tasks
- `Subagent`: submits verdicts and explanations
- `Runner`: appends system entries
- `ActivityService`: writes activity entries and reports

Primary actions:

- append a note
- append a verdict
- record a stage report
- read task history during review or recovery

### Non-Subagent Author

The non-subagent author of an activity entry.

Set by:

- `ActivityService` or CLI code when the author is not a subagent role

Used by:

- activity rendering and filtering

- Preferred: `non-subagent author`
- Python type: `NonSubagentAuthor`

```python
class NonSubagentAuthor(str, Enum):
    OPERATOR = "operator"
    SYSTEM = "system"
```

### Activity Entry

One append-only entry attached to a task.

Created by:

- subagents when they submit notes or verdicts for a pipeline state
- `Operator` when leaving task comments or manual decisions
- `Runner` when recording important system-facing task history

Exists because:

- preserving the human-readable history of what happened on the task,
  including optional verdict decisions

Used after creation by:

- recovery prompts to understand recent context and verdicts
- review and CLI views to show the task history
- operators as the primary readable audit trail

- Preferred: `activity entry`
- Python type: `ActivityEntry`

```python
class ActivityEntry(BaseModel):
    author: NonSubagentAuthor | ExecutionRole
    pipeline_state: PipelineState | None = None
    verdict: StageVerdict | None = None
    message: str
    created_at: datetime
```

Why `message` is a string:

- this object exists for human-readable review history
- structured machine data should live in dedicated fields on reports and
  runtime records, not be hidden inside free-form activity text

Why this does not use a dedicated `ActivityActor` enum:

- subagent-authored entries should reuse `ExecutionRole`
- only `operator` and `system` need extra non-subagent values
- that avoids duplicating the role vocabulary in two enums

### Task Activity

The append-only history attached to a task.

Created by:

- `ActivityService` when a task first receives activity

Exists because:

- storing the readable audit trail for one task

Used after creation by:

- operators, recovery, review, and CLI/reporting views

- Preferred: `task activity`
- Python type: `TaskActivity`

```python
class TaskActivity(BaseModel):
    task_id: str
    entries: list[ActivityEntry] = Field(default_factory=list)
```

Used after creation by:

- recovery reads recent activity to understand why the previous run failed
- review and acceptance read activity to understand agent rationale
- operators inspect activity instead of searching logs first

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

Used by:

- `PipelineRunner` records it on `TaskOutcome` and `StageReport`
- recovery and retry logic read it when deciding how to route the next step
- queue and reporting code use it for machine-readable summaries and filtering

Why it exists separately from `StageVerdict`:

- `StageVerdict` is the high-level decision
- `OutcomeReasonCode` captures the more specific machine-readable reason behind
  that decision or interruption

Example:

- `verdict=REJECT` answers "was this accepted?"
- `reason_code=MERGE_CONFLICT` answers "what specifically caused the failure?"

### Failure Reason Code

A normalized reason code for terminal failures.

Set by:

- failure-handling and recovery code when a task reaches a terminal failure

Used by:

- reporting and debugging code to classify failed tasks

- Preferred: `failure reason code`
- Python type: `FailureReasonCode`

```python
class FailureReasonCode(str, Enum):
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    RECOVERY_BUDGET_EXCEEDED = "recovery_budget_exceeded"
    RECOVERY_CRASHED = "recovery_crashed"
    PRE_EXEC_RECOVERY_FAILED = "pre_exec_recovery_failed"
```

### Report Source

The origin of a stage report.

Set by:

- the code path producing the `StageReport`

Used by:

- reporting and debugging to distinguish agent-produced vs hook-produced
  reports

- Preferred: `report source`
- Python type: `ReportSource`

```python
class ReportSource(str, Enum):
    AGENT = "agent"
    HOOK = "hook"
```

### Stage Report

A structured summary of what happened in one executable pipeline state.

Created by:

- `ActivityService`, `PipelineRunner`, or report-building code when a pipeline
  state finishes

Exists because:

- storing the machine-readable summary of one state execution

Used after creation by:

- routing, reporting, debugging, and later review of state outcomes

- Preferred: `stage report`
- Python type: `StageReport`

```python
class StageReport(BaseModel):
    task_id: str
    pipeline_state: PipelineState
    verdict: StageVerdict
    source: ReportSource = ReportSource.AGENT
    summary: str
    message: str = ""
    tests_added: int = 0
    tests_passing: int = 0
    warnings: list[str] = Field(default_factory=list)
    reason_code: OutcomeReasonCode | None = None
    failure_diagnostics: FailureDiagnostics | None = None
    created_at: datetime
```

Why `StageReport` exists separately from `ActivityEntry`:

- activity is append-only conversation and review history
- a stage report is the normalized machine-readable summary used for routing,
  reporting, and later analysis

### Activity Service

The service that writes human-facing task history.

Exists because:

- task activity and stage reports should be written through one boundary

Used by:

- subagent-report submission, CLI commands, and reporting flows

- Preferred: `activity service`
- Python type: `ActivityService`

```python
class ActivityService:
    def append_entry(self, task_id: str, entry: ActivityEntry) -> None: ...
    def record_stage_report(self, report: StageReport) -> None: ...
```

## Artifacts Domain

Purpose:
this domain covers persisted byproducts of execution. These objects exist so
operators and debugging tools can inspect what happened without mixing raw
artifacts into task intent or runtime state.

Primary actors:

- `PipelineRunner`: writes session and trace artifacts
- `Runner`: appends pipeline journal entries
- `Operator`: inspects logs and traces during debugging
- `ArtifactStore`: persists raw execution byproducts

Primary actions:

- persist a session record
- append execution trace entries
- append pipeline journal entries
- write logs

### Session

The persisted metadata for one subagent run or resumable engine conversation.

Created by:

- `PipelineRunner` or artifact-writing code when a subagent session starts

Exists because:

- identifying and tracking one resumable execution session

Used after creation by:

- trace/log storage, resume logic, and debugging views

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

### Trace Entry Kind

The kind of entry inside an execution trace.

Set by:

- artifact-writing code as trace entries are appended

Used by:

- trace renderers and debugging tools

- Preferred: `trace entry kind`
- Python type: `TraceEntryKind`

```python
class TraceEntryKind(str, Enum):
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SYSTEM = "system"
```

### Execution Trace Entry

One structured entry inside an execution trace.

Created by:

- trace-writing code during subagent execution

Exists because:

- preserving one readable unit of execution output or tool activity

Used after creation by:

- execution-trace renderers and debugging tools

- Preferred: `execution trace entry`
- Python type: `ExecutionTraceEntry`

```python
class ExecutionTraceEntry(BaseModel):
    kind: TraceEntryKind
    role: str = ""
    content: str = ""
    created_at: datetime
```

### Execution Trace

A human-readable structured trace of what a subagent did or said during one
run.

Created by:

- artifact-writing code when a subagent run starts

Exists because:

- preserving the structured readable trace for one run

Used after creation by:

- operators, debugging, and post-run inspection

- Preferred: `execution trace`
- Python type: `ExecutionTrace`

```python
class ExecutionTrace(BaseModel):
    entries: list[ExecutionTraceEntry] = Field(default_factory=list)
```

### Event Stream Entry

One normalized engine or adapter event.

Created by:

- engine adapters as structured events are emitted

Exists because:

- preserving low-level engine or adapter events in normalized form

Used after creation by:

- debugging, observability, and adapter-level analysis

- Preferred: `event stream entry`
- Python type: `EventStreamEntry`

```python
class EventStreamEntry(BaseModel):
    kind: str
    message: str = ""
    created_at: datetime
```

### Event Stream

The structured sequence of per-run events emitted by an engine or adapter.

Created by:

- artifact-writing code when an engine run starts

Exists because:

- storing the normalized event history for one run

Used after creation by:

- debugging and observability tooling

- Preferred: `event stream`
- Python type: `EventStream`

```python
class EventStream(BaseModel):
    entries: list[EventStreamEntry] = Field(default_factory=list)
```

### Journal Entry

One machine-generated pipeline journal row.

Created by:

- `Runner` as the pipeline advances

Exists because:

- preserving one transition or pipeline event in append-only form

- Preferred: `journal entry`
- Python type: `JournalEntry`

```python
class JournalEntry(BaseModel):
    seq: int
    created_at: datetime
    pipeline_state: PipelineState
    event_name: str
    message: str = ""
```

Used after creation by:

- `Runner` appends it while moving through the pipeline
- debugging tools read it to reconstruct transitions
- operators inspect it when task activity is too high-level

### Pipeline Journal

The append-only machine-generated history of pipeline states and pipeline
events.

Created by:

- journaling code when a task first enters the pipeline

Exists because:

- storing the full machine-readable transition history for one task

Used after creation by:

- debugging, CLI inspection, and transition analysis

- Preferred: `pipeline journal`
- Python type: `PipelineJournal`

```python
class PipelineJournal(BaseModel):
    entries: list[JournalEntry] = Field(default_factory=list)
```

### Log Artifact

Raw operational output intended mainly for debugging or operator inspection.

Created by:

- runtime and artifact-writing code during task and subagent execution

Exists because:

- preserving raw operational output that is too low-level for activity or
  reports

- Preferred: `log artifact`
- Python type: `LogArtifact`

```python
class LogArtifact(BaseModel):
    kind: str
    path: Path
```

Used after creation by:

- operators inspect it when a run crashed or behaved unexpectedly
- recovery may use it as evidence during diagnosis
- it is lower-level than activity, reports, and journal entries

### Artifact Store

The service that persists execution byproducts.

Exists because:

- traces, journals, sessions, and logs should share one persistence boundary

Used by:

- `PipelineRunner`, `Runner`, and artifact-writing code

- Preferred: `artifact store`
- Python type: `ArtifactStore`

```python
class ArtifactStore:
    def write_session(self, session: Session) -> None: ...
    def append_trace_entry(self, session_id: str, entry: ExecutionTraceEntry) -> None: ...
    def append_journal_entry(self, task_id: str, entry: JournalEntry) -> None: ...
    def write_log(self, task_id: str, artifact: LogArtifact) -> None: ...
```

## Configuration Domain

Purpose:
configuration is operator-controlled input. This domain exists to keep static
settings separate from mutable runtime state.

Primary actors:

- `Operator`: writes workspace configuration
- `ConfigService`: loads, validates, and applies defaults

Primary actions:

- load config
- validate config
- provide defaults to runtime services

### Litehive Config

Operator-controlled configuration for the workspace.

Created by:

- the operator in workspace config files

Exists because:

- providing declarative settings to runtime services

Used after creation by:

- startup, engine selection, recovery selection, and other runtime services

- Preferred: `litehive config`
- Python type: `LitehiveConfig`

```python
class LitehiveConfig(BaseModel):
    recovery_engine: EngineName | None = None
```

### Config Service

The service that loads and validates workspace configuration.

Exists because:

- config loading, defaults, and validation should be centralized

Used by:

- CLI startup and runtime services that need validated config

- Preferred: `config service`
- Python type: `ConfigService`

```python
class ConfigService:
    def load(self, workspace: Path) -> LitehiveConfig: ...
    def validate(self, config: LitehiveConfig) -> None: ...
```
