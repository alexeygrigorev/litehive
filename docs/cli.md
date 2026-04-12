# CLI Reference

This page covers the full `litehive` command surface as of the current
repository state.

If you installed the local wrapper, use `litehive ...`. Otherwise run the same
commands as `uv run litehive ...`.

## Command Groups

Top-level commands:

- `status`
- `health`
- `engine`
- `queue`
- `task`
- `import`
- `repair`
- `start`
- `stop`
- `restart`
- `run`
- `rollback`
- `report`
- `worktree`
- `archive`

## Workspace Setup And Inspection

Litehive bootstraps `.litehive/` on first run. Use `litehive status` in a fresh repo,
then edit `.litehive/config.yaml` and `.litehive/context.md` by hand.

### `litehive status`

Show workspace status.

```bash
litehive status
litehive status --fast
litehive status --full
```

Options:

- `--fast`: state-first summary without runtime hydration
- `--full`: full per-task dump

### `litehive queue`

Show the active task and queued order.

```bash
litehive queue
```

## Engine Management

### `litehive engine`

Manage engine freezes and print a compact engine summary.

```bash
litehive engine freeze codex --until 2026-04-08
litehive engine unfreeze codex
litehive engine status
```

`litehive engine freeze` stores `engine_freeze.<name>` in `.litehive/config.yaml`
using a UTC ISO timestamp derived from the provided `YYYY-MM-DD` date.
`litehive engine status` prints one compact line with the workspace
`default_engine`, current `engine_freeze` entries, and a per-engine capability
summary.

## Task Creation

### `litehive task add`

Create a queued task.

Implementation-style task:

```bash
litehive task add "Fix queue ordering bug" \
  --goal "Dependency-blocked tasks do not jump ahead of runnable work." \
  --acceptance-criteria "Blocked tasks remain visible but are not selected before prerequisites finish." \
 
```

Typed task:

```bash
litehive task add "Write admin guide" --task-type docs
```

Useful options:

- `--goal`
- `--acceptance-criteria` (repeatable)
- `--depends-on`
- `--task-type`
- `--mode full|single`

### `litehive import spec`

Create a rough task from a freeform specification using an engine.

```bash
litehive import spec spec.md
cat notes.txt | litehive import spec
litehive import spec spec.md --engine gemini --model gemini-2.5-pro
```

### `litehive import issue`

File upstream Litehive work from another project.

```bash
litehive import issue \
  --upstream "engine timeout not handled correctly" \
  --type runtime_bug \
  --details "Observed during recovery in project X." \
 
```

Patch handoff example:

```bash
litehive import issue \
  --upstream "improve codex timeout handling" \
  --type engine_adapter_fix \
  --patch-branch recover/codex-timeout-fix \
  --prepare-patch-branch \
 
```

## Task Editing

### `litehive task update`

Update task metadata after creation.

```bash
litehive task update T-0002 --title "Clarify queue recovery state machine"
```

Replace durable shaping fields:

```bash
litehive task update T-0002 \
  --title "Clarify final queue recovery behavior." \
  --goal "Clarify final done-state for queue recovery." \
  --acceptance-criteria "Interrupted tasks resume at the preserved stage." \
  --constraint "Keep changes scoped to queue state handling." \
  --plan-step "Inspect stale-runner recovery paths." \
 
```

Other supported patterns:

```bash
litehive task update T-0002 --depends-on T-0001,T-0003
litehive task update T-0002 --depends-on none
litehive task update T-0002 --from-file task-shape.yaml
litehive task update T-0002 --edit
```

Record an engine switch request and requeue the task for the next pass.

```bash
litehive switch T-0002 gemini --reason "quota exhausted"
```

### `litehive close`

Close a task with an explicit non-implementation outcome.

```bash
litehive task close T-0007 --outcome wont_do --reason "superseded by T-0011"
litehive task close T-0008 --outcome deferred --reason "revisit after release"
litehive task close T-0009 --outcome duplicate --follow-up-task T-0004
```

## Queue Management

### `litehive queue move`

Move a queued task to a 1-based position.

```bash
litehive queue move T-0004 1
```

To reorder several tasks, combine `litehive queue promote` and
`litehive queue move` instead of a dedicated prioritize command.

### `litehive queue promote`

Promote one queued task to the front.

```bash
litehive queue promote T-0006
```

### `litehive queue requeue`

Requeue a flagged or closed task.

```bash
litehive queue requeue T-0006
litehive queue requeue T-0006 --front
```

### `litehive queue resume`

Resume an interrupted, parked, flagged, or closed task from its current stage.

```bash
litehive queue resume T-0006
litehive queue resume T-0006 --front
```

### `litehive task abandon`

Cancel a flagged or closed task and remove it from the queue.

```bash
litehive task abandon T-0006
```

## Running Work

### `litehive run`

Run the next task once:

```bash
litehive run
```

Drain the pool:

```bash
litehive run --drain
```

Preview selection without invoking agents:

```bash
litehive run --dry-run
litehive run --drain --dry-run
```

Override the run engine:

```bash
litehive run --engine gemini --model gemini-2.5-pro
```

Useful pool controls:

- `--stop-on-failure`
- `--max-tasks`
- `--stop-on-dirty-git`

### `litehive queue stop`

Stop the current active task cleanly.

```bash
litehive queue stop
```

## Recovery And Diagnostics

### `litehive repair`

Repair stale active tasks, interrupted runs, and queue inconsistencies.

```bash
litehive repair
```

### `litehive health`

Report whether dirty git state should block the pool and explain ownership.

```bash
litehive health
```

### `litehive rollback`

Revert a task checkpoint commit and requeue the task.

```bash
litehive rollback T-0010
```

### `litehive queue requeue`

Requeue a completed task without reverting code.

```bash
litehive queue requeue T-0010
```

## Reporting From Agents

### `litehive report`

Submit a stage verdict for the active task or an explicit task id.

```bash
litehive report \
  --verdict pass \
  --role swe \
  --step implementing \
  --message "Implemented the documentation set and verified the files exist." \
  --files-changed docs/README.md \
  --files-changed docs/cli.md \
 
```

Allowed verdicts:

- `pass`
- `fail`
- `reject`
- `blocked`
- `comment`

Useful options:

- `--role`
- `--step`
- `--files-changed` (repeatable)
- `--task-id`

## Daemon Commands

### `litehive start`

Start the workspace daemon.

```bash
litehive start
```

### `litehive status`

Show the registered daemon PID and recent workspace-local logs.

```bash
litehive status
```

### `litehive stop`

Stop the workspace daemon.

```bash
litehive stop
```

### `litehive restart`

Restart the workspace daemon.

```bash
litehive restart
```

## Common Workflows

### Start a new workspace

```bash
litehive status
litehive status
```

### Add and run a task

```bash
litehive task add "Implement feature X" \
  --goal "..." \
  --acceptance-criteria "..." \
 
litehive run
```

### Operate a background queue

```bash
litehive start
litehive status
litehive queue
```

### Recover from interruption

```bash
litehive repair
litehive status --full
litehive queue resume T-0004
```

For workflow behavior and routing semantics, continue with
[pipeline.md](pipeline.md).
