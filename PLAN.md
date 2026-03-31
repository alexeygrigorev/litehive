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

The control loop is deterministic. LLMs or external CLIs produce work and reports,
but they do not decide routing. Routing is handled by local code based on structured
verdicts and configured retry limits.

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

v1 engine adapters:

- `codex`
- `opencode`

Planned adapter shape:

- capability detection
- command construction
- transcript capture
- structured report extraction

Later adapters can extend the same contract for `claude`, `gemini`, and `copilot`.

## Git model

- detect the repo root from the current working tree
- record changed files and command outputs in task artifacts
- commit automatically when a task reaches acceptance success
- commit format: `litehive: complete <task-id> <slug>`

## UI model

`litehive` should resume the active task if one exists; otherwise it opens the queue/task view.

Initial UI surfaces:

- configuration flow
- queue and active task status
- implementation progress
- subagent activity and reports

## Implementation sequence

1. Package and CLI scaffold
2. Config/workspace bootstrap
3. YAML schemas and repository layer
4. Deterministic task runner
5. Engine adapter and subagent runtime
6. Textual app and default flows
7. Git integration and acceptance commit
8. Smoke tests and sample workspace
