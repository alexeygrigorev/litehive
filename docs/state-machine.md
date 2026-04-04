# Litehive Task State Machine

> **Change gate**: Any change to `_ROUTES`, `TaskStatus`, `PipelineStatus`, or
> `OutcomeReasonCode` in the codebase **must** be accompanied by a corresponding
> update to this document in the same commit.

---

## Overview

A litehive task has two orthogonal state fields:

| Field | Type | Purpose |
|-------|------|---------|
| `status` | `TaskStatus` | Queue / execution lifecycle |
| `pipeline_status` | `PipelineStatus` | Which stage the task is at |

Together they give the full picture: a task is `in_progress` at `implementing`,
or `queued` at `testing` because a previous run was interrupted and it is waiting
for another pass.

Queue-affecting transitions are persisted atomically. When Litehive claims a task,
finishes a run, requeues or resumes work, abandons or closes a task, or recovers a
completed/interrupted task, it writes the task record, task runtime, and workspace
queue state as one transition under the workspace lock. If one of those writes
fails, Litehive restores the pre-transition files instead of leaving
`active_task_id`, queue membership, and task status out of sync.

---

## TaskStatus — queue and execution lifecycle

| Value | Meaning |
|-------|---------|
| `queued` | Waiting in the pool to be picked up, including resumable interrupted, flagged, or rejected work |
| `in_progress` | Currently running under the pool runner |
| `interrupted` | Execution stopped by runner or subagent termination; resumable from the preserved stage |
| `done` | Completed successfully; git checkpoint recorded |
| `flagged` | Blocked, unresolvable, or interrupted by an unhandled error — needs human attention |
| `cancelled` | Explicitly abandoned or execution-cancelled without a PM close outcome |
| `wont_do` | Explicit PM close: the task will not be implemented |
| `deferred` | Explicit PM close: the task is intentionally parked for later reconsideration |
| `duplicate` | Explicit PM close: the task is covered elsewhere |

Terminal states (no automatic forward progress): `done`, `cancelled`, `wont_do`, `deferred`, `duplicate`.

`interrupted`, `flagged`, and explicitly closed tasks can be resumed via `litehive resume`.
`flagged` and explicitly closed tasks can be requeued via `litehive requeue` if they should re-enter execution from the implementation entry stage.
`interrupted`, `flagged`, and explicitly closed tasks can be permanently abandoned via `litehive abandon`.
Any non-done task can be explicitly closed via `litehive close --outcome <code>`.
System-interrupted and flagged tasks are also returned to the runnable queue automatically unless the interruption reason came from an explicit CLI stop. User-stopped tasks stay parked until resumed manually.

---

## PipelineStatus — stage position

Stages run in fixed order:

```
backlog → grooming → implementing → testing → accepting → commit_to_git → done
```

| Stage | Role | Description |
|-------|------|-------------|
| `backlog` | — | Task created but not yet started |
| `grooming` | Planner | PM-style planning: clarify the user problem, define acceptance criteria, decompose scope, and produce a plan |
| `implementing` | SWE | Write the code change |
| `testing` | QA | Verify the change passes tests and review |
| `accepting` | Reviewer | PM-style final review: validate the end-user outcome and decide done versus not-done |
| `commit_to_git` | Runner | Record git checkpoint commit |
| `done` | — | Task complete |

When a task re-enters a stage after a flagged or system-interrupted run, litehive uses a dedicated `recovery` role for stage execution in `implementing`, `testing`, and `accepting`.
The recovery agent inspects the recorded failure context, artifacts, and continuation handoff, then makes whatever code or task-state changes are needed to restore a runnable path and finish the task.

---

## Verdicts

Each stage execution produces one verdict:

| Verdict | Meaning |
|---------|---------|
| `pass` | Stage succeeded; advance to next stage |
| `accept` | Synonym for `pass` (used by accepting role) |
| `fail` | Stage failed; see transition table for routing |
| `reject` | Reviewer explicitly rejected; see transition table |
| `blocked` | Stage cannot proceed (missing criteria, dependency, quota) |

---

## Transition table

Source: `_ROUTES` dict in `litehive/runner.py`.

| Stage | Verdict | Next stage | Notes |
|-------|---------|------------|-------|
| grooming | pass / accept | implementing | |
| grooming | blocked | — (flagged) | Missing acceptance criteria |
| implementing | pass / accept | testing | |
| testing | pass / accept | accepting | |
| testing | fail / reject | implementing | Task is requeued at `implementing` for another runnable pass; counts against retry limit |
| accepting | pass / accept | commit_to_git | |
| accepting | fail / reject | implementing | Task is requeued at `implementing` for another runnable pass; counts against retry limit |
| commit_to_git | pass / accept | done | |
| commit_to_git | fail / reject / blocked | — (flagged) | Commit failed |

### Rejection requeue and retry limit

When `testing` or `accepting` returns `fail` or `reject`, the task routes back to
`implementing`, persists `status = queued`, and returns control to the pool with
`final_status = queued`. The task remains runnable and is picked up on a later pass.
Each such rejection increments the rejection counter, and that counter persists across
requeued runs.

The retry limit is resolved in order: task-level override → workspace default.
`max_retries=N` allows exactly N rejections before the limit is enforced.

When the rejection counter exceeds `max_retries` (i.e. `rejections > max_retries`),
the runner terminates the task as `flagged` with
`reason_code = "retry_limit_exhausted"` instead of requeuing it again.

---

## Terminal outcomes

When a task exits the pipeline without reaching `done`, it records an `OutcomeKind`
and an `OutcomeReasonCode`.

### OutcomeKind

| Kind | Meaning |
|------|---------|
| `flagged` | Blocked, unresolvable, or interrupted by an unhandled error — requires human action |
| `blocked` | A specific dependency or criteria check prevented execution |
| `interrupted` | Execution stopped externally but kept enough runtime context to resume deterministically |
| `cancelled` | Deliberately stopped |
| `wont_do` | Explicit PM close: the task will not be implemented |
| `deferred` | Explicit PM close: the task is intentionally parked for later reconsideration |
| `duplicate` | Explicit PM close: the task is covered elsewhere |

### OutcomeReasonCode

| Code | Kind | Trigger |
|------|------|---------|
| `verdict_fail` | flagged | Stage produced `fail` and no retry route was available |
| `verdict_reject` | flagged | Stage produced `reject` and no retry route was available |
| `verdict_blocked` | blocked | Stage produced `blocked` |
| `missing_acceptance_criteria` | blocked | Grooming passed but criteria were missing when entering implementing |
| `execution_interrupted` | interrupted | Runner `KeyboardInterrupt`, stale-runner recovery, or subagent termination interrupted the current stage and preserved resumable context |
| `execution_cancelled` | cancelled | Runner interrupted mid-stage and requeued the task, or `litehive abandon` closed it |
| `stage_exception` | flagged | Unhandled Python exception during stage execution; the task stays queued with failure context recorded |
| `unsupported_verdict` | flagged | Stage returned a verdict not in the transition table |
| `wont_do` | wont_do | Task explicitly closed with `status = wont_do` |
| `deferred` | deferred | Task explicitly closed with `status = deferred` |
| `duplicate` | duplicate | Task explicitly closed with `status = duplicate` |

---

## Intentional non-implementation outcomes

Tasks that will not be implemented through the normal pipeline should be closed
explicitly rather than left in `flagged` or silently abandoned:

```
litehive close T-0042 --outcome wont_do  --reason "Superseded by T-0039" --follow-up-task T-0039
litehive close T-0043 --outcome deferred --reason "Revisit after v2 release"
litehive close T-0044 --outcome duplicate
```

This records the decision in the task journal, persists the rationale and outcome code
and optional follow-up task link on the task record, sets `task.status` to the chosen
close outcome, and removes the task from the queue while keeping it visible in
status/reporting and pool summaries.

---

## Acceptance-Criteria Gate

Litehive treats some tasks as "larger tasks" that must carry structured acceptance
criteria before implementation starts. The current requirement signals are:

- dependencies
- an explicit goal
- high priority
- a multi-step plan

When one of those signals is present and `acceptance_criteria` is empty, the task
cannot proceed into `implementing`. The runner blocks the transition, and task
metadata changes or recovery paths that would otherwise place the task back at an
implementation-entry stage reroute it to `grooming` until at least one structured
criterion is persisted. During `grooming`, the planner can provide explicit
`ACCEPTANCE_CRITERIA` bullets, or the runner can infer and persist them from the
current task context when that context is already specific enough.

---

## Parking / pausing

A task is "parked" by the human-checkpoint mechanism. When a task opt into
`before_acceptance` or `before_commit`, the runner sets `status = queued` and
`pipeline_status = <next stage>` before returning `paused`. The task stays in the
queue at the boundary stage and will not advance until the pool is restarted and
a human confirms the run.

Runner interruptions and stale-runner/subagent termination recovery move the task
to `status = interrupted`. Litehive preserves the current `pipeline_status`,
records `runtime.last_outcome.kind = interrupted`, keeps the interrupted stage in
`runtime.current_stage`, snapshots the last known subagent context when available,
and stops the pool with `task_interrupted`. The task is visible as resumable until
`litehive resume <id>` returns it to `status = queued` at the preserved stage
subject only to normal reroutes such as missing acceptance criteria.
System-triggered interruptions are also reinserted into the runnable queue automatically so the pool can pick them up again without manual repair; explicit CLI stops stay parked.

QA or reviewer rejection also parks the task in the runnable pool. In that case the task
switches back to `pipeline_status = implementing`, keeps `status = queued`, records
the rejection report with `retry_decision = retry`, and re-enters the pool for the
next implementation pass instead of moving to a sink state.

Flagged tasks likewise return to the runnable queue automatically. When they are claimed again,
`commit_to_git` failures resume at `commit_to_git`, while other flagged tasks restart from the
appropriate implementation-entry stage with their failure context preserved so the recovery agent
can continue from the real problem instead of starting from a blank slate.

For `commit_to_git`, recoverability is anchored to the last successful pre-commit review stage,
not to the latest report overall. That means a task remains resumable at `commit_to_git` even after
the most recent `commit_to_git-*.yaml` report recorded a failed integration attempt such as a
cherry-pick or merge conflict. Litehive treats that as "review passed, integration still pending",
so the pool can retry or repair the final integration step instead of dropping the task back into
an earlier stage.

---

## Cancellation

| Trigger | Status | Reason code |
|---------|--------|-------------|
| KeyboardInterrupt during run | `interrupted` | `execution_interrupted` |
| `litehive abandon <id>` | `cancelled` | `execution_cancelled` |
| `litehive close <id> --outcome wont_do` | `wont_do` | `wont_do` |
| `litehive close <id> --outcome deferred` | `deferred` | `deferred` |
| `litehive close <id> --outcome duplicate` | `duplicate` | `duplicate` |

---

## Done

A task is `done` when:

1. `commit_to_git` returns `pass` or `accept`, **and**
2. `task.status = "done"` and `task.pipeline_status = "done"` are persisted, **and**
3. A git checkpoint commit is recorded (unless `auto_commit = false`).

Checkpoint policy:

- Default checkpoint subject: `litehive: complete <task-id> <task-slug>`
- Checkpoints are created at `commit_to_git`
- Generated checkpoint subjects keep the default base subject and append ` (attempt N)` on reruns after `rollback` or `recover`
- Task-level or workspace-level auto-commit can disable checkpoint creation explicitly; otherwise `done` requires a recorded checkpoint commit
- `rollback` records which attempt was reverted, then requeues the task at the implementation entry stage
- `recover` clears the recorded checkpoint pointer without reverting code, then requeues the task at the implementation entry stage
- If structured acceptance criteria are still required, recovery reroutes to `grooming` as the implementation entry stage instead of `implementing`

---

## Runner Status Model

Litehive tracks runner health through a durable `RunnerStatusState` record written
to `.litehive/.runner.lock` whenever a pool run is active.

| Field | Type | Purpose |
|-------|------|---------|
| `status` | `RunnerExecutionStatus` | Current health classification |
| `pid` | `int \| None` | OS PID of the runner process |
| `workspace` | `str` | Resolved workspace path |
| `command` | `str` | Command-line invocation |
| `started_at` | `str \| None` | ISO-8601 timestamp when the runner acquired the lock |
| `heartbeat_at` | `str \| None` | ISO-8601 timestamp of the last heartbeat refresh |
| `active_task_id` | `str \| None` | Task ID currently being executed |

### RunnerExecutionStatus

| Value | Meaning |
|-------|---------|
| `idle` | No runner is active and no workspace reconciliation is needed |
| `running` | Runner holds the OS lock and heartbeat is current |
| `late` | Runner holds the OS lock but heartbeat has not been refreshed within the threshold (default: 60 seconds); runner may be hung |
| `stale` | Lock is not held but workspace state still shows a running task; pending reconciliation |

### Heartbeat

While a task is executing, `runner_heartbeat` starts a background thread that
refreshes `heartbeat_at` in the lock file every second. The heartbeat is written
atomically under a per-lock `metadata_lock`.

When the heartbeat context exits (normally or via exception), it clears
`active_task_id` from the metadata so status reads after task completion
correctly show `idle` once the runner guard releases the lock.

### Reconciliation

`_reconcile_stale_runner_tasks` is called before every task selection
(`peek_next_task_selection`, `dequeue_next_task_selection`, `plan_task_selections`,
`restore_untouched_active_task`). It:

1. Requeues tasks whose `execution_status = running` but which are not the current
   `active_task_id` (orphaned running tasks that the lock holder is not tracking).
2. Requeues the active task itself when its `execution_status = running` but the
   runner lock is no longer held (crash or SIGKILL recovery).
3. Stranded `commit_to_git` tasks are recovered via the existing commit-recovery
   path rather than plain requeue.

Reconciliation writes to task YAML and the journal but does not mutate the
runner lock file directly; that cleanup happens in `runner_status()` when
conditions allow.



| Command | Effect |
|---------|--------|
| `litehive requeue <id>` | Re-adds a `flagged` or explicitly closed task to the queue at the implementation entry stage |
| `litehive resume <id>` | Re-adds an `interrupted`, `flagged`, or explicitly closed task to the queue at its preserved stage, subject to normal reroutes |
| `litehive rollback <id>` | Reverts the checkpoint commit and requeues the task at `implementing` |
| `litehive recover <id>` | Requeues a completed task at `implementing` without reverting code |
