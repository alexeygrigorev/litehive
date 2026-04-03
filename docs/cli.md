# litehive CLI

This document explains the main `litehive` CLI flows, with emphasis on task
creation, updates, dependencies, queue control, and execution.

## Core idea

The CLI is the operator surface for a local deterministic task runner.

- create and shape tasks
- express dependencies between tasks
- inspect queue and runtime state
- resume, requeue, close, or repair work
- run one task at a time or drain the queue through the wrapper

## Common commands

- `litehive configure`
- `litehive status`
- `litehive queue`
- `litehive add "<title>"`
- `litehive update T-0001 ...`
- `litehive move T-0001 1`
- `litehive prioritize T-0003 T-0002 T-0001`
- `litehive promote T-0001`
- `litehive requeue T-0001 --front`
- `litehive resume T-0001 --front`
- `litehive close T-0001 --outcome wont_do --reason "..."`
- `litehive repair`
- `litehive run`
- `scripts/run-all.sh .`

`--workspace` defaults to the current directory in normal repo-local use.

## Creating tasks

Generic runnable task:

```bash
litehive add "Investigate queue stalls" \
  --goal "Explain why queue and status commands slow down on large workspaces." \
  --acceptance-criteria "The root cause is identified and documented."
```

Typed task:

```bash
litehive add "Review adapter update" --task-type review
```

Task with PM sizing:

```bash
litehive add "Stabilize commit recovery" \
  --pm-complexity moderate \
  --planned-effort m
```

Task with dependencies:

```bash
litehive add "Implement queue graph view" --depends-on T-0010
```

Notes:

- Generic tasks now require a minimum spec at creation time: a non-empty `--goal` and at least one `--acceptance-criteria`.
- Typed tasks can still be created from templates with `--task-type`; litehive will seed goal, criteria, constraints, and plan from the template.
- Use `litehive intake` when the request is still a messy brain dump and needs planner refinement before it becomes runnable work.
- If the scope or constraints are unclear, stop and ask the user for the missing information instead of creating a blank backlog item.
- PM rule: runnable backlog items should enter the queue with a real goal and explicit acceptance criteria; grooming should refine that spec, not invent it from a title alone.

Recommended patterns:

```bash
litehive add "Stabilize commit recovery" \
  --goal "Make commit_to_git idempotent and resumable after interruptions." \
  --acceptance-criteria "Successful commit_to_git retries do not duplicate commits." \
  --acceptance-criteria "A resumed task can finish commit_to_git without manual cleanup."

litehive add "Review adapter update" --task-type review

litehive intake spec.md
```

## Updating tasks

Examples:

```bash
litehive update T-0001 --engine opencode
litehive update T-0001 --pm-complexity complex --planned-effort l
litehive update T-0002 --depends-on T-0001,T-0003
litehive update T-0002 --depends-on none
```

`--depends-on` can be repeated or passed as a comma-separated list.
Use `none` to clear dependencies on update.
Use `litehive update` to add missing goal, acceptance criteria, or dependencies before requeueing legacy tasks:

```bash
litehive update T-0001 --goal "Clarify the exact done-state for queue recovery."
litehive update T-0001 --acceptance-criteria "Queued interrupted tasks are rerouted through planner before implementation."
```

Recommended PM grooming flow:

```bash
litehive update T-0001 \
  --goal "Normalize underspecified queued, interrupted, and flagged tasks before implementation retries." \
  --acceptance-criteria "Tasks missing a usable goal or acceptance criteria are routed through planner before implementation." \
  --acceptance-criteria "Planner grooming persists the normalized goal and acceptance criteria back to the task record." \
  --acceptance-criteria "Legacy tasks are not allowed to re-enter implementing with a blank task spec."
```

Use that flow when:

- the task was created too early and only has a title
- acceptance criteria changed after discussion
- dependencies or constraints became clearer during grooming

SWE startup expectation:

- pull execution context from the task folder first
- read `task.yaml`, the latest report, and the latest rejection or recovery artifact before exploring broad repo context
- if those fields are missing or contradictory, bounce the task back through grooming or recovery instead of guessing

## Dependencies

Dependencies are durable task metadata stored in `depends_on`.

What they do:

- a task that depends on another task is not runnable until the prerequisite task is `done`
- queue selection is dependency-aware
- blocked tasks stay visible without being incorrectly picked too early

Example:

```bash
litehive add "Parallel task execution" --depends-on T-0130,T-0075
```

In that case:

- `T-0130` and `T-0075` must be completed first
- `litehive` will keep the dependent task in the queue
- but it will not claim it while prerequisites are unfinished

Dependency rules:

- no self-dependency
- no missing task ids
- no dependency cycles

## Queue control

Move a task to a specific position:

```bash
litehive move T-0001 1
```

Prioritize a set of tasks to the front in the given order:

```bash
litehive prioritize T-0003 T-0002 T-0001
```

Promote one task to the front:

```bash
litehive promote T-0001
```

Important:

- queue order still respects dependency constraints
- a dependent task at the front of the queue can still remain unpicked if its blockers are not done

## Recovering and closing work

Requeue a task for another implementation pass:

```bash
litehive requeue T-0001 --front
```

Resume a task from its current preserved stage:

```bash
litehive resume T-0001 --front
```

Repair stale workspace state:

```bash
litehive repair
```

Close a task explicitly:

```bash
litehive close T-0001 --outcome wont_do --reason "Superseded by T-0039"
litehive close T-0002 --outcome deferred --reason "Revisit after release"
litehive close T-0003 --outcome duplicate
```

## Running work

Run one task:

```bash
litehive run
```

Preview the next run:

```bash
litehive run --dry-run
```

Use the wrapper for continuous execution:

```bash
scripts/run-all.sh .
```

The wrapper restarts `litehive run` each iteration and writes per-iteration logs
under `.litehive/logs/run-all/`.

## Status and queue inspection

Short status:

```bash
litehive status
```

Queue listing:

```bash
litehive queue
```

What status is meant to answer quickly:

- active task id
- current stage
- queue size
- stop reason
- current engine

## Current limitation

The CLI is good at creating tasks and updating common scalar fields, but richer
human task-shaping flows are still improving. Planner grooming can already persist
structured task updates during execution; richer operator-side CLI shaping is the
subject of backlog work rather than a complete solved surface today.
