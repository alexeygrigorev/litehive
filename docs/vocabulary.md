# Vocabulary

This document defines the preferred product and codebase vocabulary for
Litehive.

It is intentionally normative:

- prefer the terms here in user-facing CLI help, docs, prompts, and code
  comments
- avoid historical labels when a clearer current term exists
- do not use `v2` in user-facing language

Some current file names and APIs still use older terms. This document describes
the target vocabulary, not necessarily the current implementation everywhere.

## Core Objects

### Workspace

The repository root managed by Litehive.

- Preferred: `workspace`
- Avoid: `repo` when the meaning is specifically the Litehive-managed working
  area rather than git in general

### Task

A unit of work tracked by Litehive.

- Preferred: `task`
- Use `task record` when you mean the durable task metadata object
- Use `task runtime` when you mean the live execution state for a task

### Queue

The ordered list of tasks waiting to run.

- Preferred: `queue`
- Use `active task` for the currently running task
- Use `queued task` for a task waiting in the queue

### Subagent

One external engine execution launched by Litehive for a task stage.

- Preferred: `subagent`
- Use `subagent run` when emphasizing one concrete execution attempt

## Lifecycle Terms

### Stage

A user-facing major step in task execution.

Target Python type:

- `CoreStage(str, Enum)` or `TaskStage(str, Enum)`

Current stage names include:

- `grooming`
- `implementing`
- `testing`
- `accepting`
- `commit_to_git`
- `recovering`

Use `stage` in docs and CLI when talking about the major task flow.

### Phase

An internal lifecycle node or transition point inside or around a stage.

Target Python type:

- `LifecyclePhase(str, Enum)`

Examples:

- `before_implementing`
- `implementing`
- `after_implementing`
- `ready`
- `worktree_sync`

Use `phase` only when the internal state-machine distinction matters.

### Node

The executable state-machine node that owns the behavior for one lifecycle
state.

- Preferred: `node`
- Avoid using `node` as a synonym for `stage`

Target Python types:

- `LifecycleNode` for the behavior class
- `PipelineState(str, Enum)` for the state name that routes to a node

Use:

- `stage` for business workflow steps
- `phase` for wrapper states like `before_*` / `after_*`
- `node` for the implementation object that runs work

### Pipeline State

The full lifecycle machine state, including non-stage states.

- Preferred: `pipeline state` when the full machine-state set is meant
- Avoid calling this just `stage`

Target Python type:

- `PipelineState(str, Enum)`

Examples:

- `ready`
- `before_implementing`
- `implementing`
- `after_implementing`
- `recovering`
- `done`
- `failed`

### Event

A state-machine signal emitted by a node or runner and consumed by the
transition table.

- Preferred: `event`
- Use `lifecycle event` when you need to distinguish it from generic logs or
  UI events
- Target Python type:
  - base class: `LifecycleEvent`
  - concrete subclasses such as `PassEvent`, `RejectEvent`, `BlockedEvent`,
    `CrashEvent`, `TimeoutEvent`, `RecoverySucceededEvent`

Examples:

- `Pass`
- `Reject`
- `Blocked`
- `Crash`
- `Timeout`
- `RecoverySucceeded`

### Trigger Event

The lifecycle event that caused Litehive to enter recovery or another special
handling path.

- Preferred: `trigger event`
- Use `trigger event kind` for the normalized stored label such as `crash`,
  `timeout`, `blocked`, or `retry_limit`
- Target Python types:
  - `TriggerEventKind(str, Enum)` for the normalized persisted label
  - `RecoveryTrigger` for the structured record containing `origin_stage`,
    `trigger_event_kind`, and optional diagnostics
- Avoid `runtime event`; that is too broad and can be confused with logs,
  telemetry, or general runtime state

### Pipeline Status

The task-visible lifecycle label mirrored onto the durable task record.

- Preferred: `pipeline status`
- Use when distinguishing it from top-level task status
- Target Python type:
  - `PipelineStatus(str, Enum)`

Use `pipeline status` only for the task-facing projection on `TaskRecord`.
Use `pipeline state` for the full lifecycle machine state.

### Task Status

The high-level execution/outcome category for a task.

Target Python type:

- `TaskStatus(str, Enum)`

Current task status values include:

- `queued`
- `in_progress`
- `done`
- `flagged`
- `interrupted`
- `parked`
- `cancelled`
- `wont_do`
- `deferred`
- `duplicate`
- `merge_failed`

Use `task status` for this category, not `stage`.

### Runner Status

The execution status of the top-level Litehive runner process.

- Preferred: `runner status`
- Target Python type:
  - `RunnerStatus(str, Enum)`

Current values include:

- `idle`
- `running`
- `late`
- `stale`

### Stage Run Status

The execution status of one stage attempt within task runtime state.

- Preferred: `stage run status`
- Avoid calling this just `status`

Target Python type:

- `StageRunStatus(str, Enum)`

Examples:

- `idle`
- `running`
- `interrupted`
- `completed`

### Status

`status` is an overloaded word in the current codebase. Use it only with an
explicit owner.

Preferred forms:

- `task status`
- `pipeline status`
- `runner status`
- `subagent status`
- `worktree rescue status`
- `quota status`

Avoid:

- bare `status` in docs, field names, and CLI output when more than one kind of
  status is in scope

Target Python types:

- `TaskStatus(str, Enum)`
- `PipelineStatus(str, Enum)`
- `PipelineState(str, Enum)`
- `RunnerStatus(str, Enum)`
- `StageRunStatus(str, Enum)`
- `SubagentStatus(str, Enum)` when Heru exposes it as an enum
- `WorktreeRescueOutcome(str, Enum)` for rescue results
- `PoolStopReason(str, Enum)` for pool termination causes
- domain-specific enums for worktree/quota status instead of generic `str`

### Outcome

The durable business outcome for a task, stage report, or recovery attempt.

- Preferred: `outcome` only for durable result semantics
- Avoid using `outcome` for transient lifecycle routing or generic CLI success
  / failure

Target Python types:

- `TaskOutcomeKind(str, Enum)` for runtime task outcome categories
- `TaskCloseOutcome(str, Enum)` for explicit close dispositions such as
  `wont_do`, `deferred`, or `duplicate`
- `RecoveryDisposition(str, Enum)` for recovery result routing such as
  `resumed`, `advanced`, `done`, `failed`
- structured records such as `TaskOutcome` and `RecoveryOutcome`

Use:

- `verdict` for agent-submitted stage decisions
- `event` for lifecycle routing signals
- `outcome` for persisted result categories

### Reason

`reason` is also overloaded in the current codebase. Split it into distinct
concepts.

Preferred forms:

- `reason code` for normalized machine-readable classification
- `message` for human-readable failure text emitted by code
- `rationale` for operator or agent explanation of why something was chosen
- `blocker` for the thing preventing progress

Avoid:

- bare `reason` when the value is actually a code, message, or rationale

Target Python types:

- `OutcomeReasonCode(str, Enum)`
- `FailureReasonCode(str, Enum)` if terminal lifecycle failures need their own
  namespace
- plain `message: str`
- plain `rationale: str`

Field guidance:

- recovery trigger:
  - `trigger_event_kind`
  - `failure_message`
  - optional `failure_reason_code`
- task close / operator action:
  - `close_outcome`
  - `rationale`
- verdict entry:
  - `verdict`
  - `message`

### Failure

A concrete execution problem, not just a negative outcome.

- Preferred: `failure` for crashes, timeouts, hook failures, merge failures,
  and similar execution problems
- Use `failure fingerprint` for deduping/recovery-budget decisions
- Use `failure diagnostics` for structured evidence captured with the failure

Target Python types:

- `FailureFingerprint`
- `FailureDiagnostics`
- optional `FailureRecord`

## Reports and Discussion

### Report

A structured summary of what happened in a stage or recovery step.

- Preferred: `report`
- Use `stage report` for stage execution output
- Use `recovery outcome` for the persisted result of a recovery run
- Use `recovery report` only if a separate human-facing recovery summary is
  kept as a report artifact
- Use `report summary` for the concise narrative text inside a report

### Discussion Thread

The ordered history of human/agent discussion entries attached to a task.

- Preferred: `discussion thread`
- Acceptable short form: `thread`
- Avoid introducing new names for the same concept

Target Python type:

- `DiscussionThread`

### Discussion Entry

One item in the discussion thread.

- Preferred: `discussion entry`
- Acceptable implementation-specific legacy term: `comment`

Target Python type:

- `DiscussionEntry`

### Verdict Entry

A discussion entry that carries a stage verdict such as `pass`, `reject`, or
`blocked`.

- Preferred: `verdict entry`
- Use this when you need to distinguish verdict-bearing entries from plain
  informational discussion entries

Target Python type:

- `VerdictEntry`

### Verdict

The decision submitted by an agent or operator for a stage outcome.

Target Python type:

- `Verdict(str, Enum)`

Preferred canonical verdict values:

- `pass`
- `reject`
- `blocked`
- `comment`

Do not use alias terms like `fail` when `reject` is meant.

## Recovery Terms

### Recovery

The process of diagnosing why task execution stopped making progress and
restoring a runnable path.

- Preferred: `recovery`
- Use `recovery agent` for the agent role that performs this work

### Recovery Outcome

The persisted result of one recovery attempt.

- Preferred: `recovery outcome`
- Target Python type:
  - `RecoveryOutcome`
- Typical fields include:
  - `trigger: RecoveryTrigger`
  - `verdict: RecoveryVerdict`
  - `reason`
  - `disposition: RecoveryDisposition`
  - `recorded_at`
  - optional failure diagnostics / fingerprint

### Recovery Context

The structured payload that accompanies entry into recovery.

- Preferred: `recovery context`
- Avoid the vague name `failure context` when the payload is specifically about
  what triggered recovery

Target Python type:

- `RecoveryContext`

Typical fields include:

- `trigger: RecoveryTrigger`
- `last_rejection`
- `failure_message`
- `failure_reason_code`
- trigger-specific diagnostics such as merge-conflict files

### Repair

Deterministic workspace-level cleanup or state correction performed by Litehive
without redoing task work.

- Preferred: `repair`
- Use for stale-runner cleanup, queue cleanup, and similar infrastructure fixes

### Resume

Continue a previously interrupted or paused task.

- Preferred: `resume`

### Requeue

Put a task back into the queue for another run.

- Preferred: `requeue`

### Recover

Requeue a completed task for another implementation pass without reverting its
current code.

- Preferred: `recover`

### Rollback

Revert a task checkpoint commit and requeue the task.

- Preferred: `rollback`

## Execution Artifacts

### Session

The persisted metadata for one subagent run.

- Preferred: `session`

### Transcript

The human-readable execution transcript for a subagent run.

- Preferred: `transcript`

### Timeline

The structured event stream for a subagent run.

- Preferred: `timeline`

### Continuation

The engine-specific resume handle that allows a later run to continue an
existing session.

- Preferred: `continuation`
- Use `continuation handoff` for the durable record passed to the next run

### Journal Entry

One structured row in a journal.

- Preferred: `journal entry`
- Use `lifecycle journal entry` for pipeline transitions or lifecycle records
- Use `task journal entry` for task-local history

Target Python type:

- `JournalEntry`

### Journal

An append-only history of task or lifecycle events intended for inspection.

- Preferred: `journal`
- Use `task journal` for the task-local journal
- Use `lifecycle journal` for pipeline transition/event history

### Log

Operational output intended primarily for debugging or operator inspection.

- Preferred: `log`
- Use for daemon logs and raw stdout/stderr style artifacts
- Do not use `log` as a synonym for structured reports or discussion entries

### Transcript

The rendered conversational or event-derived execution trace for one subagent
run.

- Preferred: `transcript`
- Avoid using `log` or `journal` for this

## Storage Terms

### Configuration

Operator-controlled settings that define how Litehive should behave.

- Preferred: `configuration` or `config`
- Avoid using `config` for mutable runtime state

### Runtime State

Mutable execution state produced while Litehive runs.

- Preferred: `runtime state`

### Repo-Local State

State stored under `.litehive/` inside the repository.

- Preferred: `repo-local`

### Global Runtime Root

User-local runtime state stored under `${LITEHIVE_HOME}`.

- Preferred: `global runtime root`

### Durable Store

The persisted runtime database and related structured storage.

- Preferred: `durable store` when speaking generally
- Use `SQLite` when the implementation detail matters

## Naming Rules

- Prefer `stage` for major workflow steps and `phase` for internal state-machine
  nodes.
- Prefer `pipeline state` for the full lifecycle machine-state enum.
- Prefer `pipeline status` only for the task-facing projected label.
- Prefer `stage run status` for runtime status of a stage attempt.
- Prefer `discussion thread` and `discussion entry` over ad hoc synonyms.
- Prefer `verdict entry` when the entry carries a stage decision.
- Prefer `task status` and `pipeline status` when the distinction matters.
- Prefer `message` for human-readable text and `reason code` for normalized
  machine classification.
- Prefer `close_outcome` for explicit task-closing dispositions.
- Prefer `trigger_event_kind` for persisted recovery trigger labels.
- Prefer `recovery context` over `failure context` for recovery-entry payloads.
- Prefer `report`, `journal`, and `log` for their specific meanings; do not use
  them interchangeably.
- Do not use `v2` in user-facing language.
