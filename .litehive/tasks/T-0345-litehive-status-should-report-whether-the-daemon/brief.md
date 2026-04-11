# T-0345 'litehive status' should report whether the daemon is alive or dead

- Mode: tasks
- Task type: adapter
- PM complexity: simple
- Planned effort: s

## Goal
Today 'litehive status' shows workspace state (active_task_id, queued_tasks, pool_stop_reason, engine_monitoring) but does not report whether the background runner process is actually alive. This lets a dead daemon hide behind normal-looking output: queue sits still, no active task, no pool stop reason, and the user assumes the daemon is idle when in fact it has crashed. The user should be able to tell from 'litehive status' alone whether the daemon is running, crashed, or never started, without having to cross-check pids with 'ps'.

## Acceptance Criteria
- 'litehive status' output includes a clear liveness field for the background runner (e.g. running / stopped / dead)
- When the recorded daemon pid no longer exists, status reports it as dead rather than as running or idle
- When the daemon was never started in this workspace, status reports that distinctly from 'dead'
- tests/ covers: alive daemon, cleanly stopped daemon, stale-pid (dead) daemon, never-started workspace
- tests/ passes

## Constraints
- Keep provider-specific behavior isolated to the adapter boundary.
- Preserve deterministic workspace state and execution flow.

## Plan
- Inspect the existing adapter interface, config wiring, and invocation flow.
- Implement the adapter change close to the integration seam.
- Verify the adapter path with a focused test or representative run.

## PM Sizing
- Complexity: simple
- Planned effort: s

## Template Guidance
- State the target adapter seam, external dependency, and expected contract up front.
- Call out config, invocation, and failure-path changes explicitly.
- Prefer verification that exercises the adapter boundary rather than unrelated paths.

## Intake Notes

### Adapter Surface
- Identify the entrypoint, inputs, outputs, and external system involved.

_TBD_

### Config and Execution Path
- Note which settings, command wiring, or failure handling must change.

_TBD_

### Verification Evidence
- Capture the focused run or test that proves the adapter path works.

_TBD_
