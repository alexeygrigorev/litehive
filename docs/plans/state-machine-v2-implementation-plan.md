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

- [x] `SqlitePersistence` — reads/writes `TaskState` against
  `pipeline_task_state` (migration 0003). Round-trip covered in
  `tests/test_pipeline_v2_sqlite_adapters.py`.
- [x] `SqliteSessionStore` — reads/writes `Session` keyed by
  `(task_id, node_name, engine_name)` in `pipeline_sessions`.
  `SessionProvider` protocol + `AgentNode.run` updated to thread
  `task_id` through.
- [ ] `StubEngine` — deterministic engine for tests that returns a
  scripted `AgentVerdict` sequence and raises scripted exceptions.
- [ ] `StaticEngineSelector` — returns engines from a fixed list,
  honoring `excluded`. Used by M1 tests only.
- [x] **Two-node commit flow** — `GitCommitNode` does plain automatic
  git merge only. On conflict it raises `MergeConflict(conflict_files)`
  which the base class converts to a new
  `MergeConflictDetected(conflict_files)` event. The rule table routes
  `commit + MergeConflictDetected → merge_resolving` with an effect
  (`stash_conflict_files`) that copies the file list into
  `state.failure_context`. `merge_resolving` is registered as the
  existing `MergeAgent` singleton. Its Pass routes to `after_commit`;
  its Reject/Crash routes to `recovering`. Worktree is left in the
  unresolved state across the hand-off so the agent has conflict
  markers to edit.
- [x] `build_registry(selector, session_store, hook_runner, commit_node,
  prompt_context, hook_specs) -> NodeRegistry` — assembles all 20 nodes
  (ready, pre-exec-recovery, 10 hook phases, 4 agent stages, commit,
  recovering, done, failed). Lives in `litehive/pipeline/registry.py`.
- [x] `ReadyNode` + `PreExecRecoveryNode` placeholder system nodes so
  the entry/resume path can be exercised end-to-end.
- [x] `SubprocessHookRunner` — runs each hook as a shell command in the
  workspace root with ``LITEHIVE_TASK_ID`` / ``LITEHIVE_STAGE`` /
  ``LITEHIVE_WORKSPACE`` injected into the env. Timeout-expiry reported
  as ``ok=False`` with a marker in the output.
- [x] Retry backoff inside `AgentNode._run_with_retries`, exponential,
  bounded, configurable via ``retry_backoff_seconds`` +
  ``retry_backoff_multiplier``. ``sleep_fn`` is injectable for tests.
- [x] Nudge-on-missing-verdict: engine adapter raises
  ``NudgeRequired`` when the agent finished without submitting a
  verdict. ``AgentNode`` reissues the turn on the same session with a
  nudge-variant prompt. ``nudge_budget`` (default 1) is separate from
  ``retry_budget``. Exhaustion produces ``Crash(NudgeBudgetExhausted)``.
- [x] Five end-to-end tests (`tests/test_pipeline_v2_end_to_end.py`):
  full-mode happy path, single-mode zero-change shortcut, single-mode
  with changes routing through commit, persistence round-trip across
  runner invocations, and a reject-retry-recover flow exercising the
  RecoveryAgent. Stub engine, stub selector, stub hook runner, real
  SqlitePersistence + SqliteJournal.

### M2 — Real single-task run via daemon

Criterion: the daemon picks up a real task and runs it through v2 with
real engines, replacing `pipeline_old.TaskExecutionRunner`.

- [x] `ConfigBackedEngineSelector` — reads `LitehiveConfig.engine_preference`
  + honors `engine_freeze`. Lives in `litehive/pipeline/engines.py`.
- [x] `HeruEngineAdapter` skeleton — translates exception names into
  the v2 error taxonomy, reads a `verdict_reader` callback, updates
  Session.engine_session_id after each turn. Raises `NudgeRequired`
  if no verdict landed. Needs the factory wired up (see T-0349).

- [x] **T-0347 v1 bridge** — `litehive/pipeline/v1_bridge.py`. Reads
  pipeline_mode from TaskRecord on v2 row creation; mirrors v2 terminal
  stage back to TaskRecord status / pipeline_status (with the
  merge_failed distinction).
- [x] **T-0348 prompt serializer** — `litehive/pipeline/prompt_serializer.py`.
  Renders the structured RoleAgent dict into the engine-facing string
  with all the standard sections (header / instructions / goal /
  acceptance / plan / constraints / last_rejection / failure_context /
  conflict_files / thread / verdict instructions). No imports from
  pipeline_old.
- [x] **T-0349 HeruEngineFactory** — `litehive/pipeline/heru_factory.py`.
  HeruEngineAdapter delegates to SubagentManager.run, captures the
  before-turn timestamp, reads thread.yaml after the turn for fresh
  verdicts, raises NudgeRequired if none landed. heru exceptions
  translated into the v2 taxonomy.
- [x] **T-0350 daemon entry point** — `litehive/pipeline/orchestration.py`
  with `run_task_v2(root, task)` that wires up the full v2 stack
  (selector + sessions + persistence + journal + hook runner +
  registry) and calls `StateMachineRunner.run_task`. CLI root command
  checks the `LITEHIVE_PIPELINE_V2=1` env var and routes through v2
  when set; default behavior (env var unset) is unchanged so existing
  daemon flows are not disturbed.

**v2 is now the unconditional executor.** No env var, no opt-in — the
CLI root, `litehive run`, and `litehive run --drain` all route through
`run_task_v2` directly. To run:

```bash
litehive              # runs the next queued task via v2
litehive run          # same (explicit)
litehive daemon run   # starts the daemon loop, which subprocess-invokes
                      # litehive run per drain tick
```

Each call:

  - Dequeues the next task from the v1 queue (task.yaml is still the
    source of truth for task intent).
  - Takes `workspace_runner_guard` + `runner_heartbeat` so
    `litehive status` shows the task as active and no two runners
    trample each other.
  - Initializes the v2 state row via the v1 bridge.
  - Wires real engines via `heru_engine_factory` with
    `ConfigBackedEngineSelector`.
  - Routes `ready → worktree_sync → before_<stage>` so parked-then-
    unparked tasks pick up current main before the SWE sees them.
  - Uses `GitCommitNode` for real merges (no stub mode).
  - Syncs the terminal stage back to the v1 TaskRecord so
    `litehive status` keeps working.

Watch `litehive logs <task_id>` and the `pipeline_journal` /
`pipeline_transitions` sqlite tables for what's happening.

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
- 2026-04-11: M1 step 1 done — `SqlitePersistence` + `SqliteSessionStore`
  landed with migration 0003 (`pipeline_task_state`, `pipeline_sessions`).
  `SessionProvider` protocol now requires `task_id` so persistent stores
  can't leak session handles across tasks. 9 new round-trip tests +
  updated db_migrations tests for the new embedded migration.
- 2026-04-11: M1 steps 2+3 done — `build_registry` factory in
  `litehive/pipeline/registry.py` assembles all 20 nodes. `ReadyNode`
  and `PreExecRecoveryNode` added as placeholder system nodes.
  `tests/test_pipeline_v2_end_to_end.py` drives a synthetic task
  through the full machine via real SqlitePersistence + SqliteJournal
  and covers full mode, single mode (zero-change shortcut and
  with-changes routes), persistence round-trip, and a reject→retry→
  recover loop. 90 v2 tests green total.
- Known gap for M2: nothing populates `state.last_report` today —
  the stub engine and the agent verdict path don't write it. Need a
  way for agents to report files/tests changed so the zero-change
  shortcut guard sees real data instead of defaults.
- 2026-04-12: bootstrap integration tests landed
  (`tests/test_pipeline_v2_bootstrap.py`). They drive `run_task_v2`
  against a real tmp_path workspace with SqlitePersistence, SqliteJournal,
  SqliteSessionStore, SubprocessHookRunner, and the full node registry
  — only the engine factory is injected. Exposed and fixed the first
  real bug: `GitWorktreeSyncNode` crashed `git fetch origin` in
  workspaces without a remote. Now no-ops gracefully via a `_has_origin`
  check. 124 v2 tests green.
- 2026-04-12: **first live run against a real LLM.** Ran `litehive run`
  against `/tmp/v2-smoke` with a queued `T-0001 smoke test v2` task.
  v2 drove `ready → worktree_sync → before_grooming → grooming →
  recovering` on its own. The planner agent (codex, real LLM call)
  correctly rejected the blank task with full EXPECTED/OBSERVED/
  reproduction reasoning. The state machine routed the reject to
  `recovering` via the `grooming reject → recovering` rule; the
  `enter_recovery` effect populated `failure_context` and incremented
  `recovery_attempt[grooming] = 1`. The recovery agent then launched
  with the full context — failure_context, origin_stage, recovery_attempt,
  AND the auto-loaded thread.yaml comments — proving the serializer's
  thread hydration works. Killed the recovery codex before it burned
  more tokens. **Every major v2 design decision worked on first contact
  with reality:** dequeue, bridge, persistence, journal, selector,
  heru adapter, verdict detection, state machine routing, effect
  application, recovery flow, thread auto-load. Nothing required a
  post-hoc fix.
- 2026-04-11: M1 step 4 done — ``GitCommitNode``,
  ``SubprocessHookRunner``, retry backoff, nudge-on-missing-verdict.
  ``NudgeRequired`` exception added to the agent error taxonomy.
  ``AgentNode._run_with_retries`` rewritten with a while loop so nudges
  don't consume retries. 19 new tests: agent retry paths (9) + hooks /
  commit including real git merge with MergeAgent (10). 109 v2 tests
  green total.
- 2026-04-11: commit flow refactored to **two nodes**. ``GitCommitNode``
  now does plain automatic merge only; on conflict it raises
  ``MergeConflict(conflict_files)`` which becomes a new
  ``MergeConflictDetected`` event. The rule table routes
  ``commit + MergeConflictDetected → merge_resolving`` (the
  ``MergeAgent`` singleton, now registered) and from there to
  ``after_commit`` on Pass or ``recovering`` on Reject/Crash/Timeout.
  New ``stash_conflict_files`` effect copies the file list into
  ``state.failure_context``. No more merge-agent delegation hack inside
  a SystemNode. Registry now has 21 nodes. New e2e test covers the
  conflict → merge_resolving → after_commit → done path end to end.
- …
