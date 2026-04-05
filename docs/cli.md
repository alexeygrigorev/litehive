# CLI Reference

This page covers the full `litehive` command surface as of the current
repository state.

If you installed the local wrapper, use `litehive ...`. Otherwise run the same
commands as `uv run litehive ...`.

## Command Groups

Top-level commands:

- `configure`
- `status`
- `engine`
- `queue`
- `repair`
- `tasks`
- `web`
- `daemon`
- `add`
- `issue`
- `intake`
- `run`
- `dirty-worktree-gate`
- `rollback`
- `recover`
- `move`
- `prioritize`
- `promote`
- `requeue`
- `resume`
- `abandon`
- `stop`
- `switch`
- `close`
- `update`
- `report`

## Workspace Setup And Inspection

### `litehive configure`

Initialize `.litehive/` and optionally seed config values.

```bash
litehive configure --workspace .
litehive configure --workspace . --default-engine codex --process-profile python
```

Useful options:

- `--default-engine`
- `--process-profile`
- `--default-retry-limit`
- `--litehive-source-path`
- `--opencode-model`
- `--gemini-model`
- `--copilot-model`
- `--claude-enabled`
- `--claude-model`
- `--claude-max-turns`
- pool limit flags such as `--pool-max-tasks`
- hook flags such as `--hook`
- subagent resource-limit flags

### `litehive status`

Show workspace status.

```bash
litehive status --workspace .
litehive status --fast --workspace .
litehive status --full --workspace .
```

Options:

- `--fast`: state-first summary without runtime hydration
- `--full`: full per-task dump

### `litehive queue`

Show the active task and queued order.

```bash
litehive queue --workspace .
```

### `litehive tasks`

Open the task view TUI.

```bash
litehive tasks --workspace .
```

### `litehive web`

Serve the local queue and session monitor.

```bash
litehive web --workspace .
litehive web --workspace . --host 127.0.0.1 --port 8765
```

## Engine Management

### `litehive engine`

Persist the workspace default engine.

```bash
litehive engine codex --workspace .
litehive engine gemini --workspace .
```

## Task Creation

### `litehive add`

Create a queued task.

Implementation-style task:

```bash
litehive add "Fix queue ordering bug" \
  --goal "Dependency-blocked tasks do not jump ahead of runnable work." \
  --acceptance-criteria "Blocked tasks remain visible but are not selected before prerequisites finish." \
  --workspace .
```

Typed task:

```bash
litehive add "Write admin guide" --task-type docs --workspace .
```

Useful options:

- `--goal`
- `--acceptance-criteria` (repeatable)
- `--depends-on`
- `--human-checkpoint`
- `--task-type`
- `--mode implementation|tasks`
- `--engine`
- `--model`
- `--retry-limit`
- `--no-auto-commit`

### `litehive intake`

Create a rough task from a freeform specification using an engine.

```bash
litehive intake spec.md --workspace .
cat notes.txt | litehive intake --workspace .
litehive intake spec.md --engine gemini --model gemini-2.5-pro --workspace .
```

### `litehive issue`

File upstream Litehive work from another project.

```bash
litehive issue \
  --upstream "engine timeout not handled correctly" \
  --type runtime_bug \
  --details "Observed during recovery in project X." \
  --workspace .
```

Patch handoff example:

```bash
litehive issue \
  --upstream "improve codex timeout handling" \
  --type engine_adapter_fix \
  --patch-branch recover/codex-timeout-fix \
  --prepare-patch-branch \
  --workspace .
```

## Task Editing

### `litehive update`

Update task metadata after creation.

```bash
litehive update T-0002 --engine opencode --workspace .
litehive update T-0002 --priority high --workspace .
litehive update T-0002 --human-checkpoint before_acceptance --workspace .
```

Replace durable shaping fields:

```bash
litehive update T-0002 \
  --goal "Clarify final done-state for queue recovery." \
  --acceptance-criteria "Interrupted tasks resume at the preserved stage." \
  --constraint "Keep changes scoped to queue state handling." \
  --plan-step "Inspect stale-runner recovery paths." \
  --workspace .
```

Other supported patterns:

```bash
litehive update T-0002 --depends-on T-0001,T-0003 --workspace .
litehive update T-0002 --depends-on none --workspace .
litehive update T-0002 --from-file task-shape.yaml --workspace .
litehive update T-0002 --edit --workspace .
litehive update T-0002 --retry-limit default --workspace .
```

### `litehive switch`

Switch the task-level engine override and requeue the task for the next pass.

```bash
litehive switch T-0002 gemini --reason "quota exhausted" --workspace .
```

### `litehive close`

Close a task with an explicit non-implementation outcome.

```bash
litehive close T-0007 --outcome wont_do --reason "superseded by T-0011" --workspace .
litehive close T-0008 --outcome deferred --reason "revisit after release" --workspace .
litehive close T-0009 --outcome duplicate --follow-up-task T-0004 --workspace .
```

## Queue Management

### `litehive move`

Move a queued task to a 1-based position.

```bash
litehive move T-0004 1 --workspace .
```

### `litehive prioritize`

Move multiple queued tasks to the front in the order given.

```bash
litehive prioritize T-0003 T-0002 T-0005 --workspace .
```

### `litehive promote`

Promote one queued task to the front.

```bash
litehive promote T-0006 --workspace .
```

### `litehive requeue`

Requeue a flagged or closed task.

```bash
litehive requeue T-0006 --workspace .
litehive requeue T-0006 --front --workspace .
```

### `litehive resume`

Resume an interrupted, parked, flagged, or closed task from its current stage.

```bash
litehive resume T-0006 --workspace .
litehive resume T-0006 --front --workspace .
```

### `litehive abandon`

Cancel a flagged or closed task and remove it from the queue.

```bash
litehive abandon T-0006 --workspace .
```

## Running Work

### `litehive run`

Run the next task once:

```bash
litehive run --workspace .
```

Drain the pool:

```bash
litehive run --drain --workspace .
```

Preview selection without invoking agents:

```bash
litehive run --dry-run --workspace .
litehive run --drain --dry-run --workspace .
```

Override the run engine:

```bash
litehive run --engine gemini --model gemini-2.5-pro --workspace .
```

Useful pool controls:

- `--stop-on-failure`
- `--max-tasks`
- `--stop-on-limit`
- `--quota-threshold`
- `--budget-threshold`
- `--pool-usage-cap`
- `--pool-cost-cap`
- `--engine-usage-cap ENGINE=COUNT`
- `--engine-budget-cap ENGINE=UNITS`
- `--engine-cost ENGINE=UNITS`
- `--stop-on-dirty-git`

### `litehive stop`

Stop the current active task cleanly.

```bash
litehive stop --workspace .
```

## Recovery And Diagnostics

### `litehive repair`

Repair stale active tasks, interrupted runs, and queue inconsistencies.

```bash
litehive repair --workspace .
```

### `litehive dirty-worktree-gate`

Report whether dirty git state should block the pool and explain ownership.

```bash
litehive dirty-worktree-gate --workspace .
```

### `litehive rollback`

Revert a task checkpoint commit and requeue the task.

```bash
litehive rollback T-0010 --workspace .
```

### `litehive recover`

Requeue a completed task without reverting code.

```bash
litehive recover T-0010 --workspace .
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
  --workspace .
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

### `litehive daemon run`

Start the workspace daemon.

```bash
litehive daemon run --workspace .
```

### `litehive daemon status`

Show the registered daemon PID and recent workspace-local logs.

```bash
litehive daemon status --workspace .
```

### `litehive daemon stop`

Stop the workspace daemon.

```bash
litehive daemon stop --workspace .
```

### `litehive daemon restart`

Restart the workspace daemon.

```bash
litehive daemon restart --workspace .
```

### `litehive daemon instances`

List live Litehive daemons across workspaces from the global registry.

```bash
litehive daemon instances
```

## Common Workflows

### Start a new workspace

```bash
litehive configure --workspace .
litehive status --workspace .
```

### Add and run a task

```bash
litehive add "Implement feature X" \
  --goal "..." \
  --acceptance-criteria "..." \
  --workspace .
litehive run --workspace .
```

### Operate a background queue

```bash
litehive daemon run --workspace .
litehive daemon status --workspace .
litehive queue --workspace .
```

### Recover from interruption

```bash
litehive repair --workspace .
litehive status --full --workspace .
litehive resume T-0004 --workspace .
```

For workflow behavior and routing semantics, continue with
[pipeline.md](pipeline.md).
