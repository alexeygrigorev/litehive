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
| `queued` | Waiting in the pool to be picked up, including resumable interrupted or rejected work |
| `in_progress` | Currently running under the pool runner |
| `interrupted` | Execution stopped by runner or subagent termination; resumable from the preserved stage |
| `done` | Completed successfully; git checkpoint recorded |
| `flagged` | Blocked, unresolvable, or interrupted by an unhandled error — needs human attention |
| `cancelled` | Explicitly abandoned or execution-cancelled without a PM close outcome |
| `wont_do` | Explicit PM close: the task will not be implemented |
| `deferred` | Explicit PM close: the task is intentionally parked for later reconsideration |
| `duplicate` | Explicit PM close: the task is covered elsewhere |

Terminal states (no automatic forward progress): `done`, `interrupted`, `flagged`, `cancelled`, `wont_do`, `deferred`, `duplicate`.

`interrupted`, `flagged`, and explicitly closed tasks can be resumed via `litehive resume`.
`flagged` and explicitly closed tasks can be requeued via `litehive requeue` if they should re-enter execution from the implementation entry stage.
`interrupted`, `flagged`, and explicitly closed tasks can be permanently abandoned via `litehive abandon`.
Any non-done task can be explicitly closed via `litehive close --outcome <code>`.

---

## PipelineStatus — stage position

Stages run in fixed order:

```
backlog → grooming → implementing → testing → accepting → commit_to_git → done
```

| Stage | Role | Description |
|-------|------|-------------|
| `backlog` | — | Task created but not yet started |
| `grooming` | PM | Clarify goal, define acceptance criteria, produce plan |
| `implementing` | SWE | Write the code change |
| `testing` | QA | Verify the change passes tests and review |
| `accepting` | PM | Accept the implementation for delivery |
| `commit_to_git` | Runner | Record git checkpoint commit |
| `done` | — | Task complete |

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
criterion is persisted. During `grooming`, the PM can provide explicit
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

QA or PM rejection also parks the task in the runnable pool. In that case the task
switches back to `pipeline_status = implementing`, keeps `status = queued`, records
the rejection report with `retry_decision = retry`, and re-enters the pool for the
next implementation pass instead of moving to a sink state.

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

## Recovery

| Command | Effect |
|---------|--------|
| `litehive requeue <id>` | Re-adds a `flagged` or explicitly closed task to the queue at the implementation entry stage |
| `litehive resume <id>` | Re-adds an `interrupted`, `flagged`, or explicitly closed task to the queue at its preserved stage, subject to normal reroutes |
| `litehive rollback <id>` | Reverts the checkpoint commit and requeues the task at `implementing` |
| `litehive recover <id>` | Requeues a completed task at `implementing` without reverting code |
