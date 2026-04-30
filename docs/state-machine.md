# Task State Machine

This document describes the current LiteHive task lifecycle state machine.

The authoritative code is split between:

- `litehive/domain/common.py` for `TaskStatus`, `PipelineState`, and
  `PipelineStatus`
- `litehive/domain/task.py` for task records and terminal-state normalization
- `litehive/lifecycle/transitions.py` and `litehive/domain/lifecycle_deltas.py`
  for pipeline transitions
- `litehive/tasks/status.py` and `litehive/tasks/queue.py` for operator task
  transitions and queue eligibility

## Task Statuses

`TaskStatus` is the high-level operator-visible lifecycle category.

Execution statuses:

- `queued`: waiting in the execution queue
- `in_progress`: currently owned by a runner
- `interrupted`: execution stopped unexpectedly and may be recoverable
- `parked`: intentionally paused by an operator or system action

Terminal or non-runnable statuses:

- `done`: completed successfully
- `closed`: explicitly closed with `close_reason`
- `flagged`: requires operator attention with `flag_reason`

Removed terminal names are not task statuses. `cancelled`, `wont_do`,
`deferred`, and `duplicate` are represented as `close_reason` values when the
task status is `closed`. `merge_failed` is represented as `flag_reason` when
the task status is `flagged`.

## Pipeline State

`PipelineState` is the internal state-machine position. It is more detailed
than `TaskStatus` and includes system and hook states such as `ready`,
`worktree_sync`, `before_implementing`, `after_testing`, `commit`,
`merge_resolving`, `recovering`, `done`, and `failed`.

`PipelineStatus` is an operator-facing projection used for task display and
coarse progress reporting. It collapses detailed `PipelineState` values into
broader phases such as `grooming`, `implementing`, `testing`, `accepting`,
`commit_to_git`, `done`, and `flagged`.

`StageReport.pipeline_state` stores the explicitly named `ReportPipelineState`
projection used by reports. It is narrower than `PipelineState`: agent reports
use executable role stages, while merge and recovery reports keep explicit
`merge_resolving` and `recovering` labels. Stage report verdicts use the
canonical `pass`, `reject`, or `blocked` vocabulary; submitted aliases such as
`accept`, `fail`, `resume`, and `budget_hit` are normalized at the report
boundary.

## Normal Flow

Typical queued execution:

```text
queued -> in_progress -> done
```

The internal pipeline advances through the configured `PipelineState` sequence
for the task mode. The task status remains `in_progress` while the runner owns
the task, then becomes `done`, `flagged`, `interrupted`, or `closed` depending
on the terminal event or operator action.

## Interrupted vs Parked

Interrupted tasks:

- caused by crashes, stale runners, timeouts, or other unexpected stops
- may be restored by repair or recovery flows
- carry interruption metadata under `runtime.execution.interruption`
- should not require the operator to recreate the task

Parked tasks:

- caused by intentional operator or system pause
- stay out of normal automatic queue selection until resumed or requeued
- also carry interruption/resume metadata when a resume stage is known

## Operator Transitions

Resume:

```text
interrupted|parked|flagged|closed -> queued
```

Resume continues from the stored resumable stage when possible.

Requeue:

```text
flagged|parked|closed -> queued
```

Requeue resets the task for another implementation pass and may clear selected
runtime outcome state.

Park:

```text
queued|in_progress|interrupted|flagged -> parked
```

Park removes the task from active queue ownership and records that it should not
be restored automatically.

Close:

```text
queued|in_progress|interrupted|parked|flagged -> closed
```

Close records a `close_reason` such as `wont_do`, `deferred`, `duplicate`, or
`execution_cancelled`. A task closed as already satisfied may become `done`
instead.

## Failure And Recovery

Recoverable pipeline failures generally route through `recovering` and may
return to the failed origin stage, requeue the task, or flag it for operator
attention.

Merge failures are modeled as flagged tasks:

```text
in_progress -> flagged
flag_reason = "merge_failed"
```

Manual worktree rescue or requeue can then move the task back into a runnable
state.

## Queue Eligibility

Queue selection and repair logic are intentionally stricter than a simple
status allowlist:

- `queued` tasks are normal candidates when dependencies and future-task guards
  allow them
- `interrupted` tasks may be recoverable through repair/requeue paths
- `parked` tasks require explicit resume or requeue
- `flagged` tasks require operator or recovery-policy handling before they can
  run again
- `done` and `closed` tasks are not normal queue candidates

The concrete eligibility rules live in `litehive/tasks/queue.py` and status
transition helpers live in `litehive/tasks/status.py`.

## Storage Notes

Task intent, task state, runtime state, stage reports, recovery reports, engine
monitoring, queue state, and audit data should live in SQLite.

The only LiteHive-owned YAML file that should remain in a workspace is
`.litehive/config.yaml`. Other structured workspace state should not use YAML.
