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

---

## TaskStatus — queue and execution lifecycle

| Value | Meaning |
|-------|---------|
| `queued` | Waiting in the pool to be picked up, including resumable interrupted or rejected work |
| `in_progress` | Currently running under the pool runner |
| `done` | Completed successfully; git checkpoint recorded |
| `flagged` | Blocked or unresolvable — needs human attention |
| `cancelled` | Deliberately stopped and closed by a human decision |
| `failed` | Reserved for explicit terminal failure states; normal runner interruptions requeue instead |

Terminal states (no automatic forward progress): `done`, `flagged`, `cancelled`, `failed`.

`flagged` and `failed` tasks can be requeued via `litehive requeue` if they are recoverable.
`flagged` and `failed` tasks can be permanently closed via `litehive abandon`.
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

---

## Terminal outcomes

When a task exits the pipeline without reaching `done`, it records an `OutcomeKind`
and an `OutcomeReasonCode`.

### OutcomeKind

| Kind | Meaning |
|------|---------|
| `flagged` | Blocked or unresolvable — requires human action |
| `blocked` | A specific dependency or criteria check prevented execution |
| `cancelled` | Deliberately stopped |
| `failed` | Unhandled execution error |

### OutcomeReasonCode

| Code | Kind | Trigger |
|------|------|---------|
| `verdict_fail` | flagged | Stage produced `fail` and no retry route was available |
| `verdict_reject` | flagged | Stage produced `reject` and no retry route was available |
| `verdict_blocked` | blocked | Stage produced `blocked` |
| `missing_acceptance_criteria` | blocked | Grooming passed but criteria were missing when entering implementing |
| `execution_cancelled` | cancelled | Runner interrupted mid-stage and requeued the task, or `litehive abandon` closed it |
| `stage_exception` | failed | Unhandled Python exception during stage execution; the task stays queued with failure context recorded |
| `unsupported_verdict` | flagged / failed | Stage returned a verdict not in the transition table |
| `wont_do` | cancelled | Task explicitly closed — not worth implementing |
| `deferred` | cancelled | Task closed — work deferred to a future decision |
| `duplicate` | cancelled | Task closed — duplicate of another task |

---

## Intentional non-implementation outcomes

Tasks that will not be implemented through the normal pipeline should be closed
explicitly rather than left in `flagged` or silently abandoned:

```
litehive close T-0042 --outcome wont_do  --reason "Superseded by T-0039"
litehive close T-0043 --outcome deferred --reason "Revisit after v2 release"
litehive close T-0044 --outcome duplicate
```

This records the decision in the task journal and removes the task from the queue,
keeping the rationale visible for later review.

---

## Parking / pausing

A task is "parked" by the human-checkpoint mechanism. When a task opt into
`before_acceptance` or `before_commit`, the runner sets `status = queued` and
`pipeline_status = <next stage>` before returning `paused`. The task stays in the
queue at the boundary stage and will not advance until the pool is restarted and
a human confirms the run.

Runner interruptions park the task in the runnable pool. In that case the task
keeps `status = queued`, preserves its current `pipeline_status`, records the
terminal report under `runtime.last_outcome`, and the pool stops with
`task_requeued` so the next run can resume deterministically.

QA or PM rejection also parks the task in the runnable pool. In that case the task
switches back to `pipeline_status = implementing`, keeps `status = queued`, records
the rejection report with `retry_decision = retry`, and re-enters the pool for the
next implementation pass instead of moving to a sink state.

---

## Cancellation

| Trigger | Status | Reason code |
|---------|--------|-------------|
| KeyboardInterrupt during run | `queued` | `execution_cancelled` |
| `litehive abandon <id>` | `cancelled` | `execution_cancelled` |
| `litehive close <id> --outcome wont_do` | `cancelled` | `wont_do` |
| `litehive close <id> --outcome deferred` | `cancelled` | `deferred` |
| `litehive close <id> --outcome duplicate` | `cancelled` | `duplicate` |

---

## Done

A task is `done` when:

1. `commit_to_git` returns `pass` or `accept`, **and**
2. `task.status = "done"` and `task.pipeline_status = "done"` are persisted, **and**
3. A git checkpoint commit is recorded (unless `auto_commit = false`).

---

## Recovery

| Command | Effect |
|---------|--------|
| `litehive requeue <id>` | Re-adds a `flagged`/`failed`/`cancelled` task to the queue at its current stage |
| `litehive resume <id>` | Like requeue but explicitly targets a stage |
| `litehive rollback <id>` | Reverts the checkpoint commit and requeues the task at `implementing` |
| `litehive recover <id>` | Requeues a completed task at `implementing` without reverting code |
