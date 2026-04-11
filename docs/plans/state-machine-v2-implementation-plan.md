# Pipeline v2 — Implementation Plan

Companion to `state-machine-overhaul.md` (the design). This doc is the
running to-do for turning the clean-slate pipeline at `litehive/pipeline/`
into the actual executor, replacing `litehive/pipeline_old/`.

## Decisions locked in

- **Prompt serialization**: v2 writes its own serializer from the
  four-layer instruction structure (`_base.py._assemble_instruction_layers`).
  **Do not** wrap `pipeline_old/agents/_prompts.py:stage_prompt`; keep v2
  self-contained so the old pipeline can be deleted in one cut.
- **No `litehive run2` CLI**. Wire v2 into the existing daemon code path;
  iterate live and fix as issues surface.
- **Commit flow is simple**: try `git merge`, success → Pass, conflict →
  delegate to `MergeAgent` once, success → Pass, else → Reject → recovering.
  No cherry-pick detection, no auto-commit toggle.
- **Dirty-worktree pre-exec gate is dropped**. Session continuation
  handles "dirty" state as a feature, not a defect. The
  `recovering_pre_exec` node only fires for genuinely broken state
  (missing worktree, corrupted sqlite row, stale lock) and gets a
  one-line probe, not a classification subsystem.
- **Retry backoff** is needed and goes inside `AgentNode._run_with_retries`.
- **Workspace heartbeat + lock** is needed before cutover.
- **Worktree sync before resume** (the `worktree_sync` node we sketched)
  is needed once tasks can park and unpark across main advancing.

## Milestones

### M1 — Sandbox run (target: one afternoon)

Criterion: `pytest -k "e2e and pipeline_v2"` drives a synthetic task
through `ready → done` with stub engines, real persistence, real journal.

- [ ] `SqlitePersistence` — reads/writes `TaskState` against a new
  sqlite table. Schema lives in migration 0003.
- [ ] `SqliteSessionStore` — reads/writes `Session` keyed by
  `(task_id, node_name, engine_name)`. Migration 0003 table.
- [ ] `StubEngine` — deterministic engine for tests that returns a
  scripted `AgentVerdict` sequence and raises scripted exceptions.
- [ ] `StaticEngineSelector` — returns engines from a fixed list,
  honoring `excluded`. Used by M1 tests only.
- [ ] `CommitNode._merge_worktree` — real git merge with delegation to
  `MergeAgent` on conflict. Takes a `git` helper injected in tests.
- [ ] `build_registry(config, selector, sessions, hook_runner) -> NodeRegistry`
  — assembles all phases (before/after × grooming/implementing/testing/
  accepting/commit), agent stages, system nodes, terminals, recovery and
  pre-exec recovery.
- [ ] `HookRunner` subprocess implementation + a `FakeHookRunner` for
  tests.
- [ ] Retry backoff inside `AgentNode._run_with_retries` (exponential,
  bounded, configurable via `retry_backoff_seconds`).
- [ ] One end-to-end test that runs a task through every agent stage,
  asserts the journal contains the expected sequence of transitions,
  and confirms final `state.stage == "done"`.

### M2 — Real single-task run via daemon

Criterion: the daemon picks up a real task and runs it through v2 with
real engines. No cutover yet; v1 still handles everything except the
specific tasks we route to v2.

- [ ] `ConfigBackedEngineSelector` — reads `LitehiveConfig.engine_preference`
  + honors `engine_freeze`. Integrates with the existing freeze auto-
  persistence on quota hit.
- [ ] `HeruEngineAdapter` — wraps `heru.get_engine("codex")` etc. to
  match the v2 `Engine` protocol. Translates heru exceptions into
  `TransientError` / `EngineBlockedError` / `UnrecoverableError`.
- [ ] V2 prompt serializer — takes the `build_prompt()` dict and
  produces the string the engine adapter wants. All four instruction
  layers, task context, thread, plan, acceptance criteria, last
  rejection, continuation handoff. Matches v1's information content
  without importing from `pipeline_old`.
- [ ] Daemon integration point — find where the current daemon invokes
  `pipeline_old.TaskExecutionRunner` and add a feature-flagged switch
  to `StateMachineRunner` instead. Flag lives in workspace config.
- [ ] Journal / TaskState translation from `TaskRecord` — an adapter
  that loads a v1 `TaskRecord` and produces a v2 `TaskState`, then
  persists back. Needed so v1 and v2 can round-trip the same task.

### M3 — Resilience features

Criterion: v2 handles parked-then-unparked tasks, restart crashes, and
multi-runner safety. Still run live against real tasks; no shadow
comparator — fix issues as they surface, not via pre-flight diffs.

- [ ] Pre-exec probe node — one-line check for "worktree exists, lock
  is free". Emits `CleanState` or `NeedsPreExecRecovery`.
- [ ] `recovering_pre_exec` node — minimal implementation that clears
  stale locks and aborts partial rebases.
- [ ] `worktree_sync` node — runs `git fetch && git merge main` before
  entering the pipeline; delegates to `MergeAgent` on conflict. Rule
  table update: `ready → worktree_sync → before_grooming/
  before_implementing`. Also: on resume, if `main HEAD` has moved
  since the saved `session_ts`, route through `worktree_sync` before
  the saved stage.
- [ ] Workspace heartbeat + lock (multi-runner safety).

### M4 — Cutover

Criterion: `pipeline_old/` is no longer invoked by any code path. The
daemon runs v2 exclusively, then `pipeline_old/` is deleted.

- [ ] Delete the feature flag; daemon always uses v2.
- [ ] Delete `pipeline_old/`.
- [ ] Delete `pipeline_old`-related imports and shim modules.

## Not in scope for this plan

- Parallel task execution (the old `_parallel.py` pool runner).
- Budget ledger (`_budget.py`). Can be added as a `BudgetGuard` later.
- Pool stop conditions (`_pool_control.py`). Part of the orchestration
  layer that gets rebuilt after the pipeline cutover.
- The broader task-lifecycle unification described in
  `state-machine-parked-findings.md`.

## Working notes (live)

This section is for rolling status as we build M1. Update it as each
item lands.

- 2026-04-11: plan written, v2 core committed
  (`litehive: add pipeline state machine v2 (clean-slate)`).
- …
