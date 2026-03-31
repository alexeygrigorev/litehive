# Litehive v1 Plan

## Product model

Litehive is a local CLI/TUI workspace for one active implementation task at a time.
Task intake and implementation are distinct modes:

- `tasks`: add, split, and reprioritize queued work
- `implementation`: execute one selected task through a deterministic pipeline

Task state is stored in local YAML under `.litehive/`. A task is the schedulable unit.
Subagents are execution workers nested inside a task folder and never become top-level
queue items unless task intake explicitly creates new tasks.

## Deterministic execution model

The pipeline is fixed:

1. `grooming`
2. `implementing`
3. `testing`
4. `accepting`
5. `commit_to_git`

The control loop is deterministic. LLMs or external CLIs produce work and reports,
but they do not decide routing. Routing is handled by local code based on structured
verdicts and configured retry limits.

The canonical state machine — all states, transitions, verdicts, outcome codes, and
the change-gate rule — is documented in [`docs/state-machine.md`](docs/state-machine.md).
**Any change to `_ROUTES`, `TaskStatus`, `PipelineStatus`, or `OutcomeReasonCode`
must update that document in the same commit.**

## Persistence model

Workspace state lives in:

- `.litehive/config.yaml`: global workspace config and engine defaults
- `.litehive/state.yaml`: active task pointer and queue metadata
- `.litehive/tasks/<task-id>/task.yaml`: canonical task state
- `.litehive/tasks/<task-id>/journal.md`: append-only narrative log
- `.litehive/tasks/<task-id>/reports/*.yaml`: per-stage reports
- `.litehive/tasks/<task-id>/subagents/<subagent-id>/*`: session-local artifacts

Only the main runner mutates canonical task state. Subagents may write reports,
transcripts, and artifacts, but do not directly edit `task.yaml`.

## Engine model

Current engine adapters:

- `codex`
- `opencode`
- `gemini`
- `copilot`

Planned next:

- `claude`

Shared adapter shape:

- capability detection
- command construction
- transcript capture
- structured report extraction

The runtime also supports:

- workspace-default engine selection
- task-level engine preferences
- run-time engine override
- fallback ordering between engines when configured

## Git model

- detect the repo root from the current working tree
- record changed files and command outputs in task artifacts
- commit automatically when a task reaches acceptance success
- checkpoint format: `litehive: checkpoint <task-id> <slug>`
- repeated completion attempts append ` (attempt N)` to the checkpoint subject
- rollback format: `litehive: rollback <task-id> <slug> (attempt N)`
- rollback reverts the checkpoint commit, writes a rollback commit, and requeues the task at `implementing`
- recover requeues a completed task at `implementing` without reverting code

## UI model

`litehive` should resume the active task if one exists; otherwise it opens the queue/task view.

Initial UI surfaces:

- configuration flow
- queue and active task status
- implementation progress
- subagent activity and reports
- shell wrappers for long pool runs and status inspection

## Pool model

`litehive run` is the pool runner. It should:

- re-read queue state between tasks
- support newly added or requeued tasks during a long run
- respect stop conditions and retry policy
- leave enough logs and status artifacts to debug failures after the fact

## Implementation sequence

1. Package and CLI scaffold
2. Config/workspace bootstrap
3. YAML schemas and repository layer
4. Deterministic task runner
5. Engine adapter and subagent runtime
6. Textual app and default flows
7. Git integration and acceptance commit
8. Smoke tests and sample workspace
