# Litehive Workspace Context

## Project
- Purpose: build a local-first deterministic coding workspace with YAML-backed task state and subagent execution.
- Main package/module locations: `litehive/cli.py`, `litehive/config.py`, `litehive/tasks.py`, `litehive/runner.py`, `litehive/runtime.py`, `litehive/subagents.py`, `litehive/engines.py`, `litehive/tui/app.py`.
- Tests: `tests/test_workspace.py`.

## Commands to know
- `uv run pytest -q`
- `uv run litehive configure --workspace .`
- `uv run litehive status --workspace .`
- `uv run litehive queue --workspace .`
- `uv run litehive add "<title>" --workspace .`
- `uv run litehive move T-0002 1 --workspace .`
- `uv run litehive promote T-0002 --workspace .`
- `uv run litehive requeue T-0002 --front --workspace .`
- `uv run litehive update T-0002 --engine opencode --priority high --workspace .`
- `uv run litehive run --workspace .`
  Drains the live task pool until no active or queued task remains, re-reading queue state between tasks.
- `scripts/run-all.sh .`
  Restarts `uv run litehive run` each iteration and writes timestamped logs under `.litehive/logs/run-all/`.
- `scripts/run-all-status.sh .`
  Shows current `litehive status` plus the latest run-all log directory and recent output.

## Engines
- Implemented adapters: `codex`, `opencode`, `gemini`, `copilot`.
- Planned later: `claude`.
- Engine selection precedence: run override, then task preference, then workspace default.

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
- If you add a new workflow or command, document it here so future runs inherit the same context.
