# Pipeline

Litehive runs work through a fixed stage pipeline and keeps routing decisions in
local code rather than in agent prompts.

## Two Run Modes

### Single-task mode

`litehive run` executes one selection cycle:

```bash
litehive run
```

Use this when you want tight operator control, are validating a new engine
setup, or want to inspect the workspace after each task.

### Full pool mode

`litehive run --drain` keeps selecting tasks until Litehive reaches a stop
condition:

```bash
litehive run --drain
```

Typical stop conditions include:

- queue exhausted
- blocked tasks remaining
- explicit pool limits reached
- dirty-worktree gate triggered
- human checkpoint reached
- operator-configured stop-on-failure behavior

Dry-run either mode without invoking agents:

```bash
litehive run --dry-run
litehive run --drain --dry-run
```

## Stages

Every runnable task advances through the same ordered stages:

```text
backlog -> grooming -> implementing -> testing -> accepting -> commit_to_git -> done
```

### grooming

Owner: `planner`

Purpose:

- clarify the task
- refine acceptance criteria
- add plan steps and constraints
- determine whether the task is ready for implementation

### implementing

Owner: `swe`

Purpose:

- perform the code or docs change
- add focused tests when appropriate
- leave a clear implementation report

### testing

Owner: `qa`

Purpose:

- verify the implementation independently
- run focused checks
- reject the task if the evidence is weak or behavior is wrong

### accepting

Owner: `reviewer`

Purpose:

- compare the delivered result against acceptance criteria
- decide whether the task is actually done
- send it back if the task is incomplete or risky

### commit_to_git

Owner: Litehive runner

Purpose:

- create the final checkpoint commit
- integrate task worktree changes back into main
- push if a remote is configured

## Roles

The shared role model is:

- `planner` owns grooming
- `swe` owns implementation
- `qa` owns verification
- `reviewer` owns final PM-style acceptance
- `recovery` is used when a failed or interrupted task needs bounded repair

The orchestrator is the manager. It selects tasks, routes stages, applies retry
policy, and decides when to escalate or stop. Subagents do not self-route.

## Task Selection

Selection is deterministic and local. By default, the pool uses the
`dependency_aware` policy, which means:

- queued tasks remain visible
- dependency-blocked tasks are not selected too early
- active and resumable task state is read from `.litehive/`

Inspect the queue with:

```bash
litehive queue
litehive status
litehive status --full
litehive status --fast
```

## State Machine Summary

A task has two main lifecycle fields:

- `status`: queue/execution lifecycle such as `queued`, `in_progress`,
  `interrupted`, `parked`, `flagged`, or `done`
- `pipeline_status`: current stage such as `implementing` or `testing`

Common states:

- `queued`: waiting to be picked
- `in_progress`: currently running
- `interrupted`: run was cut off but can be resumed
- `parked`: intentionally stopped by an operator
- `flagged`: blocked or failed hard enough to need intervention
- `done`: finished and checkpointed
- `wont_do`, `deferred`, `duplicate`: explicit non-implementation outcomes

Authoritative transition details live in [state-machine.md](state-machine.md).

## Stage Verdicts

Each stage reports one verdict:

- `pass`
- `accept`
- `fail`
- `reject`
- `blocked`

Routing rules:

- `grooming pass` -> `implementing`
- `implementing pass` -> `testing`
- `testing pass` -> `accepting`
- `accepting pass` -> `commit_to_git`
- `commit_to_git pass` -> `done`
- `testing fail/reject` -> back to `implementing`
- `accepting fail/reject` -> back to `implementing`
- `grooming blocked` -> `flagged`
- `commit_to_git fail/reject/blocked` -> `flagged`

## Retry And Escalation

Litehive distinguishes between normal iteration and terminal failure.

### Review rejection loop

If `testing` or `accepting` rejects the task:

- the task is requeued at `implementing`
- the rejection counter increases
- the task remains visible and runnable

### Global retry limit

If total review rejections exceed the effective retry limit:

- the task is marked `flagged`
- reason code becomes `retry_limit_exhausted`

### Per-stage retry escalation

If the same review stage keeps rejecting:

- Litehive reroutes the task to `grooming`
- the task stays runnable
- continuation context is recorded so the planner can re-scope the work

## Human Checkpoints

Tasks can pause before important boundaries:

```bash
litehive task update T-0007 --edit
```

When a checkpoint is reached, the pool stops cleanly and leaves the task queued
at the next stage.

## Daemon Execution

The daemon wraps the same pool logic but repeats it in the background:

```bash
litehive start
litehive status
litehive stop
```

A daemon iteration:

1. runs `litehive repair`
2. runs one fresh `litehive run` subprocess
3. records logs in `.litehive/logs/run-all/`
4. stops when Litehive reports an explicit stop reason

## Operator Controls

Useful intervention commands:

```bash
litehive queue stop
litehive queue resume T-0003
litehive queue requeue T-0003
litehive task abandon T-0003
litehive task close T-0003 --outcome deferred --reason "waiting on upstream dependency"
```

The main principle is: tasks that cannot make progress right now should stay
visible and resumable instead of disappearing.
