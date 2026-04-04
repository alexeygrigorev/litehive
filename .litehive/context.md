# Litehive Workspace Context

## Project
- Purpose: build a local-first deterministic coding workspace with YAML-backed task state and subagent execution.
- Main package/module locations: `litehive/cli.py`, `litehive/config.py`, `litehive/tasks.py`, `litehive/runner.py`, `litehive/runtime.py`, `litehive/subagents.py`, `litehive/engines.py`, `litehive/tui/app.py`.
- Tests: `tests/test_workspace.py`.

## Commands to know
- `uv run pytest -q`
- `uv run litehive configure --workspace .`
- `uv run litehive status --workspace .`
- `uv run litehive status --workspace . --fast`
  Uses state-first reads and skips per-task runtime hydration for a quicker workspace summary.
- `uv run litehive web --workspace .`
  Starts a local-only HTTP monitor for queue state, task/session details, tailed artifacts, and recent run-all logs.
- `uv run litehive queue --workspace .`
- `uv run litehive repair --workspace .`
- `uv run litehive add "<title>" --workspace .`
- `uv run litehive add "<title>" --task-type review --workspace .`
  Defaults to `mode: tasks` for typed intake so the task folder gets `brief.md` template guidance; pass `--mode implementation` to opt out.
- `uv run litehive intake <file> --workspace .`
  Creates a rough task from a freeform brain dump or specification using an LLM.
- `uv run litehive move T-0002 1 --workspace .`
- `uv run litehive prioritize T-0004 T-0002 T-0003 --workspace .`
- `uv run litehive promote T-0002 --workspace .`
- `uv run litehive requeue T-0002 --front --workspace .`
- `uv run litehive resume T-0002 --workspace .`
- `uv run litehive abandon T-0002 --workspace .`
- `uv run litehive update T-0002 --engine opencode --priority high --workspace .`
- `uv run litehive update T-0002 --human-checkpoint before_acceptance --workspace .`
- `uv run litehive run --workspace .`
  Runs the next active or queued task once and leaves remaining work queued.
- `uv run litehive run --workspace . --dry-run`
  Shows the next planned task, selected engine, and predicted stop reason without invoking any agents.
- `uv run litehive run --workspace . --drain`
  Drains the live task pool until no active or queued task remains, re-reading queue state between tasks.
- `uv run litehive run --workspace . --drain --dry-run`
  Shows the planned pool order, selected engines, and predicted stop reason without invoking any agents.
- `scripts/run-all.sh .`
  Restarts `uv run litehive run` each iteration and writes timestamped logs under `.litehive/logs/run-all/`.
- `scripts/run-all-status.sh .`
  Shows current `litehive status` plus the latest run-all log directory and recent output.

## Engines
- Implemented adapters: `codex`, `opencode`, `gemini`, `copilot`.
- Planned later: `claude`.
- Engine selection precedence: run override, then task preference, then workspace default.

## Process decisions
- The intended execution loop is: `grooming -> implementing -> testing -> accepting -> commit_to_git`.
- QA rejection should return the same task to SWE; it should not become terminal failure.
- PM rejection should return the same task to SWE; it should not go back to backlog.
- Review rejection is normal iteration, not a final task outcome.
- `failed` is not the desired long-term lifecycle state for review rejection; state-machine cleanup is planned.
- Tasks that cannot be worked on right now should be parked or requeued, not dropped.
- Explicit non-implementation outcomes such as `wont_do`, `deferred`, or `duplicate` should stay visible with rationale.
- Any future state-machine change should update the durable state-machine documentation in the repo.
- Workspace locking should become granular: short atomic locks for active-task transitions, but queue intake and non-conflicting queue updates should remain possible while a runner is active.
- Interrupted runners and subagents should become resumable states with recorded context rather than silent stale `running` state.
- `litehive repair` is the manual recovery entrypoint for stale active tasks, interrupted runs, and queue cleanup.

## Development rules
- Keep changes scoped to the current task.
- Prefer targeted tests over broad test suites.
- Use local YAML state under `.litehive/` as the source of truth.
- Do not invent extra persistence layers unless the task requires it.
- Prefer committed task outputs so completed changes are easy to revert and recover.

## Tool usage
- `codex` is currently used for self-hosted testing.
- `opencode` should run with model `zai-coding-plan/glm-5.1`.
- `gemini` and `copilot` adapters exist, but their usage should still be validated through real task runs.
- `claude` support is deferred and should remain opt-in because quota is limited.
- When claude support is added, prefer Claude Sonnet for testing rather than Opus to reduce quota usage.
- `opencode` must not inherit provider credential env vars; the adapter strips the same variables as the `oc()` wrapper from `~/.bashrc`.
- Execution visibility is a first-class requirement: task runs should expose current stage, subagent status, transcript/output, and recent progress clearly.
- Long pool runs should leave durable per-iteration logs so failures can be diagnosed after the fact.
- Live subagent state should be written to disk while the subagent is still running, not only after completion.
- Tasks may opt into human checkpoints before acceptance or commit; those pauses should stop the pool cleanly and leave the task queued at the next stage.
- If you add a new workflow or command, document it here so future runs inherit the same context.
