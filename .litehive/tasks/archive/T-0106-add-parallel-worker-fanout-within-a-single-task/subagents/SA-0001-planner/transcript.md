I’m reviewing the current orchestration and subagent execution paths to define scope and concrete acceptance criteria for parallel worker fanout inside one task. Next I’ll inspect the runner, runtime, and subagent/task models to see what already exists and where the design pressure is.
The codebase already has single-active-subagent runtime state and deterministic stage routing, so parallel fanout will cut across runtime persistence, runner orchestration, and observability rather than being a prompt-only change. I’m checking the current task record plus the nearby planned tasks to separate what belongs in T-0106 from later integration/worktree/merge work.
The queued follow-ups confirm the intended split: T-0106 should establish fanout orchestration and visibility, while deterministic integration is already separated into T-0107. I’m checking the prerequisite task and the current runtime data model to make sure the acceptance criteria stop short of integration/worktree concerns.
The main constraint is explicit now: runtime and CLI/TUI observability assume one `active_subagent` and one `last_subagent`. I’m validating those surfaces so the grooming output can call out the required slice of work and keep T-0106 scoped to fanout plus visibility, not full integration.
VERDICT: PASS
SUMMARY: Scope is clear enough to implement parallel SWE-worker fanout inside a single task by extending runtime state, orchestration, and observability, while leaving deterministic output integration to T-0107.
FILES_CHANGED:
- none
TESTS_ADDED: 0
TESTS_PASSING: 0
WARNINGS:
- The current runtime model is single-worker only (`active_subagent`/`last_subagent`), so this task is not a small prompt change; it needs coordinated updates in runtime persistence, stage execution, CLI/TUI observability, and focused tests.
- Keep T-0106 scoped to fanout orchestration, persisted worker state, and visibility. Do not fold deterministic worker-output integration into this task; that is already split into T-0107.
- T-0108 is cancelled, so the initial implementation should assume workers run inside the existing task execution checkout rather than introducing per-worker worktrees here.
- Recommended implementation slice: add multi-worker runtime state and status rendering first, then fanout/wait orchestration for `implementing`, then interruption/recovery handling, then targeted tests.
FOLLOW_UP_TASKS:
[]
PM_COMPLEXITY: complex
PLANNED_EFFORT: l
ACCEPTANCE_CRITERIA:
- The `implementing` stage can launch more than one SWE worker for a single task from a deterministic local plan, and the runner does not advance the task to `testing` until all launched workers have reached a terminal state.
- Litehive persists per-worker runtime state and artifacts while fanout is in progress, including worker identity, role/engine, status, path, and recent progress, so a live run no longer collapses to a single `active_subagent`.
- Task observability surfaces parallel worker state clearly in status/reporting output, showing multiple active or recently finished workers for the same task instead of only one active/last worker.
- If a fanout run is interrupted or a worker fails, the task remains recoverable at the same stage with enough persisted worker context to diagnose or resume/retry deterministically; it must not silently leave ambiguous single-worker runtime state.
- Focused tests cover successful multi-worker fanout for one task, persisted live worker state, status/summary rendering for multiple workers, and interrupted or failed fanout behavior.

[stderr]
Reading additional input from stdin...
