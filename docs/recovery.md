# Recovery

Litehive treats interruption and failed progress as a normal part of the system,
not as an exceptional one-off.

The recovery model has three layers:

1. repair workspace state
2. resume or requeue tasks
3. launch recovery or merge-resolution agents when a bounded fix is possible

## First Response: `litehive repair`

If a prior run crashed, was interrupted, or left stale queue state behind, start
with:

```bash
litehive repair
```

`repair` is the manual entrypoint for:

- stale active task cleanup
- interrupted-run recovery
- queue/state reconciliation
- stranded `commit_to_git` cleanup
- flagged-task recovery back to a runnable stage when it is safe

## Resumable Task States

These states can still move forward:

- `interrupted`
- `parked`
- `flagged`
- explicit close states such as `wont_do`, `deferred`, and `duplicate` if you
  decide to resume or requeue them

Main controls:

```bash
litehive queue resume T-0008
litehive queue requeue T-0008 --front
litehive task abandon T-0008
```

Use `resume` when you want to continue from the task's current preserved stage.
Use `requeue` when you want to send it back through the normal implementation
entry path.

## Recovery Agents

When a stage fails in a recoverable way, Litehive can launch a `recovery` agent.
The recovery agent:

- reads the task record and latest reports
- inspects recovery evidence collected from `.litehive/`
- applies only the smallest safe repair needed to restore a runnable path
- reports whether the task is runnable again or still blocked

Recovery artifacts are stored under the task's `recovery/` directory alongside
the normal reports and comment history.

## What Evidence A Recovery Agent Sees

Litehive gathers recovery evidence from task-local artifacts such as:

- latest stage report
- runtime state from the workspace database
- `comments.yaml`
- `events.jsonl`
- latest subagent `session.yaml`
- latest subagent `report.yaml`
- recent daemon or run-all log when available

This is why recovery should start from task-local artifacts rather than broad
repo exploration.

## Commit Recovery

`commit_to_git` is special because the code change may already exist while final
integration is failing.

If commit or merge fails, Litehive:

1. records the failure context
2. launches a recovery agent when possible
3. attempts to finish the checkpoint and integration
4. leaves the task flagged only if no safe bounded repair was found

Typical commit-recovery problems:

- merge conflicts while integrating a task worktree
- dirty state left in the worktree
- stale or partial worktree commits

## Merge Resolution

Litehive uses an agent-assisted merge-resolution flow in two places:

1. `git pull --rebase` on main before integrating a task worktree
2. merging the task worktree back into main during `commit_to_git`

If conflicts appear, Litehive launches a merge-resolution agent with narrow
instructions:

- read the conflicting files
- remove conflict markers
- keep the correct content
- stage only the resolved files
- finish the rebase or merge without unrelated edits

If conflicts remain unresolved after the recovery attempt, Litehive aborts the
merge or rebase and records the task as blocked or failed rather than silently
continuing.

## Rollback And Recover For Completed Tasks

Two commands bring a completed task back into the queue:

### `rollback`

```bash
litehive rollback T-0009
```

This reverts the recorded checkpoint commit and requeues the task.

Use it when the completed task should be undone in the repository before a new
implementation pass.

### `recover`

```bash
litehive queue requeue T-0009
```

This requeues the completed task without reverting workspace code.

Use it when you want another pass on top of the current repository state.

## Engine Switch Recovery

If an engine runs out of quota or becomes unsuitable mid-task, switch it without
losing history:

```bash
litehive task update T-0004 --engine gemini
```

Litehive records the switch in task artifacts and keeps continuation pointers so
the next pass has the right context.

## Dirty Worktree Gate

Use this before long pool runs if you suspect unowned local changes:

```bash
litehive health
```

The report explains whether dirty git state should block the pool and whether
the changes belong to the main checkout or a task worktree.

## Human Intervention Cases

Automatic recovery is intentionally bounded. Expect manual intervention when:

- a failure needs product clarification rather than code repair
- retry or stage-retry limits were exhausted
- the workspace is missing required credentials or tools
- merge conflicts cannot be resolved safely by the agent
- Litehive self-heal points to a bug in the Litehive repo but
  `litehive_source_path` is missing or unusable

In those cases, the right move is usually:

```bash
litehive repair
litehive status --full
litehive queue
```

Then decide whether to resume, requeue, close, or file upstream Litehive work
with `litehive import issue`.
