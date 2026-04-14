# Litehive Codebase Deletion Plan

Date: 2026-04-14

## Baseline

- Main codebase size: `22,383` Python LOC under `litehive/`
- Largest packages:
  - `lifecycle`: `4,318`
  - `cli`: `4,195`
  - `tasks`: `2,985`
  - `agents`: `2,356`
  - `state`: `1,505`
  - `config`: `1,494`
  - `observability`: `1,245`

## Bottom Line

Two different targets are realistic:

1. Keep all current behavior and CLI surface:
   - realistic reduction: about `20-30%`
   - mostly from deleting duplicate state handling, duplicate transition code, duplicate lock/status paths, and generalized recovery scaffolding
2. Get close to `50%`:
   - requires narrowing the supported surface
   - specifically: fewer execution modes, fewer operator/diagnostic surfaces, one sandbox backend, one status path, one queue policy, and much less self-healing logic

If “without losing any functionality” means preserving every current CLI command, every recovery path, every observability path, and every sandbox mode, then halving the codebase is not realistic. If it means preserving the core product behavior of queuing tasks, running agents, recovering obvious failures, and committing results, then it is achievable, but only after product-scope decisions.

## Big Wins First

### 1. Remove duplicated task transition logic

The same state transitions are implemented twice in `tasks/status.py`.

- Dedicated commands:
  - `requeue_task()` in [litehive/tasks/status.py](/home/alexey/git/litehive/litehive/tasks/status.py:274)
  - `abandon_task()` in [litehive/tasks/status.py](/home/alexey/git/litehive/litehive/tasks/status.py:397)
  - `close_task()` in [litehive/tasks/status.py](/home/alexey/git/litehive/litehive/tasks/status.py:443)
  - `park_task()` in [litehive/tasks/status.py](/home/alexey/git/litehive/litehive/tasks/status.py:511)
- The same logic is re-implemented in `update_task()`:
  - outcome branch at [litehive/tasks/status.py](/home/alexey/git/litehive/litehive/tasks/status.py:585)
  - action branch at [litehive/tasks/status.py](/home/alexey/git/litehive/litehive/tasks/status.py:629)
- There is even a duplicate alias at [litehive/tasks/status.py](/home/alexey/git/litehive/litehive/tasks/status.py:750)

Plan:

- Keep one internal transition function per real state change.
- Make CLI/report entry points thin wrappers.
- Delete the duplicated branch bodies inside `update_task()`.

Estimated deletion: `220-320` LOC.

### 2. Collapse to one runtime source of truth

Right now Litehive carries task execution state in two different models and bridges between them.

- Pipeline state machine state in [litehive/lifecycle/persistence.py](/home/alexey/git/litehive/litehive/lifecycle/persistence.py:50)
- Task runtime/state fields in [litehive/domain/task.py](/home/alexey/git/litehive/litehive/domain/task.py:76)
- Separate task runtime mutation helpers in [litehive/tasks/runtime.py](/home/alexey/git/litehive/litehive/tasks/runtime.py:24)
- Bridge code that initializes one model and syncs back into the other in [litehive/lifecycle/orchestration.py](/home/alexey/git/litehive/litehive/lifecycle/orchestration.py:63) and [litehive/lifecycle/orchestration.py](/home/alexey/git/litehive/litehive/lifecycle/orchestration.py:84)

This is the single biggest structural simplification opportunity in the repo.

Plan:

- Pick one execution-state model.
- Either:
  - run the lifecycle directly against `TaskRecord.runtime`
  - or make task/status readers consume the lifecycle `TaskState` directly
- Delete the sync-back bridge and most of the duplicate runtime mutators.

Estimated deletion: `700-1,200` LOC, likely more once follow-on simplifications land.

### 3. Delete singleton “probe/repair” abstractions around worktree recovery

The pre-exec recovery system is generalized for multiple probes and repairs, but production wiring uses one probe and one repair.

- Probe and repair factories in [litehive/lifecycle/orchestration.py](/home/alexey/git/litehive/litehive/lifecycle/orchestration.py:179) and [litehive/lifecycle/orchestration.py](/home/alexey/git/litehive/litehive/lifecycle/orchestration.py:200)
- The generic list-based node wiring is used once at [litehive/lifecycle/orchestration.py](/home/alexey/git/litehive/litehive/lifecycle/orchestration.py:302)
- The defensive generic nodes live in [litehive/lifecycle/nodes/system.py](/home/alexey/git/litehive/litehive/lifecycle/nodes/system.py:40) and [litehive/lifecycle/nodes/system.py](/home/alexey/git/litehive/litehive/lifecycle/nodes/system.py:349)

Plan:

- Replace probe lists and repair lists with one direct `ensure_worktree_state()` check.
- Fail loudly on unexpected exceptions instead of routing everything to “needs recovery”.
- Merge pre-exec worktree cleanup into the worktree sync step.

Estimated deletion: `90-160` LOC.

### 4. Consolidate runner lock and daemon lock code

The runner lock code and daemon registry are the same pattern implemented twice.

- Runner lock metadata handling in [litehive/state/locking.py](/home/alexey/git/litehive/litehive/state/locking.py:43)
- Daemon lock metadata handling in [litehive/daemon/registry.py](/home/alexey/git/litehive/litehive/daemon/registry.py:39)
- `runner_status_readonly()` is defined twice in the same file at [litehive/state/locking.py](/home/alexey/git/litehive/litehive/state/locking.py:129) and [litehive/state/locking.py](/home/alexey/git/litehive/litehive/state/locking.py:157)

Plan:

- Build one small lockfile metadata utility.
- Share liveness checks, yaml read/write, stale clearing, and “is active” logic.
- Delete the duplicate readonly-status implementation.

Estimated deletion: `150-260` LOC.

### 5. Unify the status path

Status is implemented in too many places.

- Fast-path CLI status re-implements runner parsing and typed-state access in [litehive/main.py](/home/alexey/git/litehive/litehive/main.py:33) and [litehive/main.py](/home/alexey/git/litehive/litehive/main.py:77)
- Workspace status rendering is large in [litehive/cli/workspace.py](/home/alexey/git/litehive/litehive/cli/workspace.py:177)
- Read-only health loading is separate in [litehive/observability/status_diagnostics.py](/home/alexey/git/litehive/litehive/observability/status_diagnostics.py:88)
- `observability/status.py` adds another formatting layer at [litehive/observability/status.py](/home/alexey/git/litehive/litehive/observability/status.py:18)

Plan:

- One status snapshot builder.
- One runner-status reader.
- One formatter for concise status and one for full status.
- Delete the bespoke `_fast_runner_status()` parser in `main.py`.

Estimated deletion: `180-320` LOC`.

### 6. Make config loading strict and delete silent fallback branches

There are multiple places where invalid current-shape config is silently normalized instead of failing.

- `load_config()` silently rewrites invalid profile and pool policy values at [litehive/config/loading.py](/home/alexey/git/litehive/litehive/config/loading.py:42)
- `resolve_process_profile()` silently falls back to `generic` at [litehive/config/profiles/loader.py](/home/alexey/git/litehive/litehive/config/profiles/loader.py:39)
- Status diagnostics swallows config/model errors and returns defaults at [litehive/observability/status_diagnostics.py](/home/alexey/git/litehive/litehive/observability/status_diagnostics.py:102) and [litehive/observability/status_diagnostics.py](/home/alexey/git/litehive/litehive/observability/status_diagnostics.py:151)

Plan:

- Let invalid current config fail validation.
- Stop silently rewriting unknown settings to defaults.
- Use status diagnostics for reporting, not for hiding loader failures.

Estimated deletion: `60-140` LOC.

## High-Value Structural Cuts

### 7. Simplify sandbox support

The sandbox layer is large because it supports two backends, two git profiles, generated wrapper scripts, and adapter override handling.

- Backend split in [litehive/agents/sandbox.py](/home/alexey/git/litehive/litehive/agents/sandbox.py:144), [litehive/agents/sandbox.py](/home/alexey/git/litehive/litehive/agents/sandbox.py:185), and [litehive/agents/sandbox.py](/home/alexey/git/litehive/litehive/agents/sandbox.py:316)
- Sandbox config branching in [litehive/config/model.py](/home/alexey/git/litehive/litehive/config/model.py:19) and [litehive/config/model.py](/home/alexey/git/litehive/litehive/config/model.py:365)
- The destructive-git policy exists twice:
  - generated inline script in [litehive/agents/sandbox.py](/home/alexey/git/litehive/litehive/agents/sandbox.py:620)
  - real module in [litehive/sandbox/git_wrapper.py](/home/alexey/git/litehive/litehive/sandbox/git_wrapper.py:12)

Plan:

- Pick one backend.
- Reuse the checked-in `sandbox/git_wrapper.py` instead of generating a second copy as a string.
- Keep one adapter path instead of wrapping both `run` and `run_live` with capability probing unless a specific engine still requires it.

Estimated deletion: `350-700` LOC.

This is one of the clearest places where getting near `50%` requires a product choice.

### 8. Collapse worktree repair, rescue, and inspection into one owner

Worktree recovery logic is spread across runtime recovery and CLI support.

- Runtime recovery/interruption logic in [litehive/recovery/workspace_repair.py](/home/alexey/git/litehive/litehive/recovery/workspace_repair.py:50)
- Rescue/inspection logic in [litehive/cli/worktree_support.py](/home/alexey/git/litehive/litehive/cli/worktree_support.py:57)

Plan:

- Keep one worktree service module.
- Keep one concept of “missing worktree”, “already landed”, and “manual conflict”.
- Make CLI code pure presentation.

Estimated deletion: `250-500` LOC.

### 9. Reduce queue-selection configurability

Queue selection currently supports multiple policies plus repair logic for broken queue state.

- Policy switching in [litehive/tasks/queue.py](/home/alexey/git/litehive/litehive/tasks/queue.py:420)
- Auto-restoring missing queued tasks in [litehive/tasks/queue.py](/home/alexey/git/litehive/litehive/tasks/queue.py:453)
- Silent config fallback for invalid policy in [litehive/config/loading.py](/home/alexey/git/litehive/litehive/config/loading.py:47)

Plan:

- Keep `dependency_aware` only.
- Delete invalid-policy fallback.
- Stop auto-healing queue membership during selection; repair it explicitly.

Estimated deletion: `120-260` LOC.

## Optional Cuts Needed To Reach ~50%

These are not just refactors. They narrow the supported surface.

- Keep one daemon command surface.
  - `cli/runner.py` duplicates daemon commands that already exist in [litehive/cli/daemon_cli.py](/home/alexey/git/litehive/litehive/cli/daemon_cli.py:24)
- Trim operator tooling.
  - `observability`, `attention`, backup/db helpers, and detailed worktree rescue are a lot of code that is not on the core task-execution path.
- Keep one status mode instead of a fast path plus full path plus diagnostics-only path.
- Consider one execution mode.
  - If the daemon is the product, simplify manual one-shot runner paths.
  - If manual `run` is the product, simplify daemon/background orchestration.

Without decisions in this section, the repo probably bottoms out well above half-size.

## Recommended Execution Order

1. Remove duplicated transition logic in `tasks/status.py`.
2. Consolidate runner/daemon lockfile code and delete duplicate status readers.
3. Make config loading strict and remove fallback-to-default branches.
4. Unify status loading/rendering and delete the bespoke fast-status parser.
5. Collapse pre-exec worktree probes/repairs into a direct check.
6. Merge worktree recovery and worktree rescue into one owner.
7. Collapse to one runtime state model.
8. Decide on sandbox scope: one backend or both.
9. Decide on product scope needed to actually hit `50%`.

## Practical Target

- Safe near-term target: delete `1.5k-2.5k` LOC without changing product scope.
- Aggressive structural target: delete `3k-5k` LOC while preserving core behavior.
- Half-size target: requires product cuts in operator tooling and support matrix, not just code cleanup.
