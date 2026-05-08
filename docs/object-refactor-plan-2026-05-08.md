# Object Refactor Plan

This checklist sequences the refactor described in:

- `docs/function-method-analysis-2026-05-08.md`
- `docs/proposed-object-structure-2026-05-08.md`
- `docs/code-style.md`

The goal is not to convert every function into a method. The goal is to move
workspace-bound, persistence-bound, and policy-bound behavior onto cohesive
objects while keeping pure utility functions free.

## Rules For Every Slice

- [ ] Start from a clean understanding of the exact old function path.
- [ ] Add or verify focused characterization tests before moving behavior.
- [ ] Introduce the new object with constructor-injected dependencies.
- [ ] Route a small set of callers to the object.
- [ ] Keep old functions only as temporary wrappers while callers migrate.
- [ ] Delete wrappers only when `rg` confirms no production callers remain.
- [ ] Run focused tests for the touched package.
- [ ] Run `make typecheck`.
- [ ] Run `make test` before any commit.
- [ ] Do not modify lint, formatter, pyrefly, ruff, or CI config.

## Phase 0: Guardrails And Baseline

- [ ] P0.1. Add a short doc note in `docs/code-style.md` that service
  extraction should happen in small tested slices with temporary wrappers only
  during caller migration.

  Verification:
  - [ ] `rg -n "temporary wrapper|tested refactor slices|constructor" docs/code-style.md`
  - [ ] `uv run pytest tests/test_architecture_guardrails.py -q`

- [ ] P0.2. Add a lightweight architecture guardrail if one does not already
  exist: new internal functions below CLI/process boundary should not accept
  raw `root: Path` when `Workspace` or a service would do.

  Verification:
  - [ ] `uv run pytest tests/test_architecture_guardrails.py -q`
  - [ ] `make typecheck`

- [ ] P0.3. Add a checklist item to this file whenever a new object is created
  and its old wrappers still exist.

  Verification:
  - [ ] Every temporary wrapper has a matching unchecked cleanup item in this
    document.

## Phase 1: Runtime Settings Repository

Reason to start here: it is cohesive, bounded, and mostly contained in
`litehive/config/runtime_settings.py`.

- [ ] P1.1. Add characterization tests for current runtime-settings behavior.

  Cover:
  - [ ] bootstrap from defaults/global/workspace config
  - [ ] load current runtime settings
  - [ ] apply runtime settings over config data
  - [ ] set setting writes audit entry
  - [ ] malformed stored JSON is tolerated where current behavior tolerates it

  Verification:
  - [ ] `uv run pytest tests/config/test_engine_freeze.py tests/config/test_loading.py -q`

- [ ] P1.2. Introduce `RuntimeSettingsRepository`.

  Target package:
  - `litehive/config/runtime_settings.py`

  Constructor dependencies:
  - `workspace: Workspace`
  - config-data loader callable
  - clock callable if needed

  Methods:
  - `bootstrap(config_data=None)`
  - `load()`
  - `apply_to_config_data(config_data)`
  - `get(key)`
  - `set(key, value, actor, source, reason=None, context=None)`
  - `audit_entries(key=None, limit=...)`

  Verification:
  - [ ] Existing tests still pass through old free-function wrappers.
  - [ ] `make typecheck`

- [ ] P1.3. Route production callers to `RuntimeSettingsRepository` through the
  container or a local repository construction at an existing boundary.

  Verification:
  - [ ] `rg -n "load_runtime_settings\\(|set_runtime_setting\\(|bootstrap_runtime_settings\\(" litehive`
  - [ ] Remaining matches are wrappers, tests, or intentionally unmigrated
    callers listed in this document.
  - [ ] `uv run pytest tests/config -q`
  - [ ] `make test`

- [ ] P1.4. Remove runtime-settings wrappers once no production callers need
  them.

  Verification:
  - [ ] `rg -n "load_runtime_settings\\(|set_runtime_setting\\(|bootstrap_runtime_settings\\(" litehive`
  - [ ] No production wrapper-only calls remain.

## Phase 2: WorkspaceTasks / TaskRepository

Reason: this removes task persistence behavior from `Workspace` and starts
shrinking `litehive/state/records.py`.

- [ ] P2.1. Add characterization tests for task repository behavior.

  Cover:
  - [ ] create task
  - [ ] list tasks with runtime
  - [ ] get task
  - [ ] require missing task error
  - [ ] save task
  - [ ] save/write/load runtime
  - [ ] discard created task
  - [ ] runtime gitignore refresh

  Verification:
  - [ ] `uv run pytest tests/state tests/tasks/test_task_persistence.py -q`

- [ ] P2.2. Introduce `WorkspaceTasks` or `TaskRepository`.

  Target package:
  - Prefer `litehive/tasks/repository.py` if the public concept is tasks.
  - Prefer `litehive/state/task_repository.py` if the implementation is still
    clearly persistence-only.

  Constructor dependencies:
  - `workspace: Workspace`
  - `runtime_store: RuntimeStore`
  - event/audit collaborators only when needed

  Methods:
  - `create(...)`
  - `list(include_runtime=True, strict=True)`
  - `get(task_id)`
  - `get_record(task_id)`
  - `require(task_id)`
  - `save(task)`
  - `discard_created(task_id)`
  - `save_runtime(task)`
  - `write_runtime(task)`
  - `load_runtime(task)`
  - `ensure_runtime_ignored()`
  - `next_task_id()`

  Verification:
  - [ ] Existing free functions delegate to the new object.
  - [ ] `make typecheck`

- [ ] P2.3. Add `tasks: WorkspaceTasks` to `LitehiveContainer`.

  Verification:
  - [ ] Container tests cover that the same workspace is injected.
  - [ ] `uv run pytest tests/cli tests/config/test_engine_freeze.py -q`

- [ ] P2.4. Route callers that already have a container to `container.tasks`.

  Verification:
  - [ ] `rg -n "Workspace\\.from_path|list_tasks_for_workspace|get_task_for_workspace|save_task_for_workspace" litehive/cli litehive/daemon`
  - [ ] Remaining matches are boundary conversions, wrappers, or listed debt.

- [ ] P2.5. Move simple `Workspace` convenience methods to wrappers over
  `WorkspaceTasks`, then remove them after callers migrate.

  Methods:
  - [ ] `Workspace.list_tasks`
  - [ ] `Workspace.get_task`
  - [ ] `Workspace.get_task_record`
  - [ ] `Workspace.require_task`
  - [ ] `Workspace.save_task`

  Verification:
  - [ ] `rg -n "\\.list_tasks\\(|\\.get_task\\(|\\.save_task\\(" litehive tests`
  - [ ] `make test`

## Phase 3: ExecutionTraceRenderer

Reason: it is mostly stateless rendering/parsing and a good low-risk service
extraction.

- [ ] P3.1. Add characterization tests for execution trace rendering.

  Cover:
  - [ ] parse unified events
  - [ ] render event
  - [ ] render from events
  - [ ] render from streams
  - [ ] render from event-stream payload
  - [ ] load subagent execution trace from artifacts/session state

  Verification:
  - [ ] `uv run pytest tests/agents -q`

- [ ] P3.2. Introduce `ExecutionTraceRenderer`.

  Target package:
  - `litehive/agents/execution_trace.py`

  Methods:
  - `parse_unified_events(stdout)`
  - `render_event(event)`
  - `render_from_events(events, stderr="")`
  - `render_from_streams(stdout, stderr)`
  - `render_from_payload(payload, stderr="")`
  - `load_for_subagent(task, ref, active=None, runtime_state=None)`

  Verification:
  - [ ] Existing free functions delegate to the renderer.
  - [ ] `make typecheck`

- [ ] P3.3. Route `SubagentManager` and session/report callers to the renderer.

  Verification:
  - [ ] `rg -n "render_execution_trace|parse_unified_events|load_subagent_execution_trace" litehive`
  - [ ] Remaining free-function calls are wrappers or tests.
  - [ ] `uv run pytest tests/agents tests/lifecycle/test_engine_adapter.py -q`

## Phase 4: TaskRuntimeTransitions

Reason: `litehive/tasks/runtime.py` has many `apply_*` and
`mark_*_for_workspace` pairs that are one domain concern.

- [ ] P4.1. Add characterization tests for every runtime transition pair.

  Cover:
  - [ ] run started/finished
  - [ ] stage started/finished
  - [ ] subagent started/progress/finished
  - [ ] engine switch
  - [ ] task outcome
  - [ ] finish run transition queue behavior

  Verification:
  - [ ] `uv run pytest tests/tasks/test_runtime_updates.py tests/config/test_engine_freeze.py -q`

- [ ] P4.2. Introduce `TaskRuntimeTransitions`.

  Constructor dependencies:
  - `workspace: Workspace`
  - `tasks: WorkspaceTasks`
  - mutation guard / lock collaborator
  - clock callable

  Methods:
  - `start_run(task)`
  - `finish_run(task, final_status)`
  - `start_stage(task, stage)`
  - `finish_stage(task, stage, status)`
  - `mark_subagent_started(task, subagent, pid=None)`
  - `mark_subagent_progress(task, subagent, execution)`
  - `mark_subagent_finished(task, subagent, result)`
  - `switch_engine(task, from_engine, to_engine, reason)`
  - `record_outcome(task, outcome)`
  - `clear_run_activity(task, execution_status)`

  Verification:
  - [ ] Existing free functions delegate to the new object.
  - [ ] `make typecheck`

- [ ] P4.3. Route lifecycle/engine adapter callers through the object.

  Verification:
  - [ ] `rg -n "mark_.*_for_workspace|finish_task_run_transition_for_workspace" litehive`
  - [ ] Remaining matches are wrappers, tests, or listed debt.
  - [ ] `uv run pytest tests/lifecycle tests/tasks -q`

## Phase 5: TaskQueueService And PoolService

Reason: queue selection/mutation and pool reporting are related but should not
be one oversized class.

- [ ] P5.1. Add characterization tests for queue selection and mutation.

  Verification:
  - [ ] `uv run pytest tests/tasks tests/cli/test_pool.py -q` if present
  - [ ] If there is no pool-specific file, run the relevant CLI/pool tests
    found by `rg -n "pool" tests`.

- [ ] P5.2. Introduce `TaskQueueService`.

  Methods:
  - `eligible_tasks()`
  - `select_next()`
  - `enqueue(task_id)`
  - `remove(task_id)`
  - `restore_missing_queued_tasks()`
  - `mark_active(task_id)`
  - `clear_active(task_id=None)`
  - `is_resumable(task)`
  - `is_runnable(task)`

  Verification:
  - [ ] Free queue functions delegate to `TaskQueueService`.
  - [ ] `make typecheck`

- [ ] P5.3. Introduce `PoolService`.

  Methods:
  - `run(limit=None)`
  - `collect_pending()`
  - `collect_resumable()`
  - `collect_closed()`
  - `summarize(progress)`
  - `render_summary(report)`
  - `stop_for(reason)`

  Verification:
  - [ ] CLI pool command only parses options and calls `PoolService`.
  - [ ] `rg -n "task_stage_outcomes_for_workspace|_pending_pool_tasks_for_workspace|_resumable_pool_tasks_for_workspace" litehive`
  - [ ] `make test`

## Phase 6: TaskReportStore, TaskActivityStore, And TaskEventLog

Reason: reports, activity, and events are related persistence surfaces but
should remain separate stores.

- [ ] P6.1. Introduce `TaskReportStore`.

  Verification:
  - [ ] Stage/recovery report tests pass through the store.
  - [ ] `rg -n "record_stage_report|load_stage_reports|record_recovery_report|load_recovery_reports" litehive`

- [ ] P6.2. Introduce `TaskActivityStore`.

  Verification:
  - [ ] `Workspace.task_activity(...)` is a temporary wrapper or no longer
    needed.
  - [ ] `rg -n "task_activity\\(" litehive tests`

- [ ] P6.3. Introduce `TaskEventLog`.

  Verification:
  - [ ] event append/read/rebuild tests pass.
  - [ ] `rg -n "append_task_event|rebuild_sqlite_from_task_event_log|task_event_log" litehive`

- [ ] P6.4. Remove `Workspace.append_event(...)` after callers use
  `TaskEventLog`.

  Verification:
  - [ ] `rg -n "\\.append_event\\(" litehive tests`
  - [ ] `make test`

## Phase 7: StatusSnapshotCollector

Reason: status currently has tolerant config/state loading and many probes.
Those belong behind one read-only collector with injected probe collaborators.

- [ ] P7.1. Add characterization tests for status with corrupt/missing inputs.

  Verification:
  - [ ] `uv run pytest tests/observability/test_status_diagnostics.py tests/observability -q`

- [ ] P7.2. Introduce `StatusSnapshotCollector`.

  Methods:
  - `collect()`
  - `collect_operational()`
  - `load_config()`
  - `load_state()`
  - `probe_runner()`
  - `probe_daemon()`
  - `probe_origin_divergence()`
  - `probe_recovery_failure()`

  Verification:
  - [ ] Existing status functions delegate to the collector.
  - [ ] `make typecheck`

- [ ] P7.3. Route CLI/status callers to the collector through the container.

  Verification:
  - [ ] `rg -n "collect_status_snapshot_for_workspace|collect_operational_status_snapshot_for_workspace" litehive`
  - [ ] `make test`

## Phase 8: EngineRoutingPolicy

Reason: default/preference/freeze/quota/recovery routing is policy, not
engine adapter lookup.

- [ ] P8.1. Add characterization tests for engine routing.

  Verification:
  - [ ] `uv run pytest tests/config/test_engine_freeze.py tests/config/test_engine_models.py -q`

- [ ] P8.2. Introduce `EngineRoutingPolicy`.

  Methods:
  - `select(task, request=None)`
  - `resolve_engine_name(name)`
  - `resolve_model_override(task, engine)`
  - `resolve_recovery_engine(task)`
  - `freeze(engine, until, reason)`
  - `unfreeze(engine, reason)`
  - `set_default(engine, reason)`
  - `set_preference(order, reason)`
  - `quota_status(engine)`
  - `clear_expired_freezes()`

  Verification:
  - [ ] Config/engine free functions delegate to `EngineRoutingPolicy`.
  - [ ] `make typecheck`

- [ ] P8.3. Route CLI and lifecycle engine selection through
  `EngineRoutingPolicy`.

  Verification:
  - [ ] `rg -n "select_engine|resolve_engine_name|resolve_recovery_engine|set_engine" litehive`
  - [ ] `make test`

## Phase 9: Worktree Service Split

Reason: `WorktreeService` is a useful facade but currently mixes sync, cleanup,
rescue, and inspection.

- [ ] P9.1. Add characterization tests for each worktree responsibility.

  Cover:
  - [ ] sync task worktree
  - [ ] inspect task worktree
  - [ ] cleanup terminal task worktree
  - [ ] collect/apply rescue candidates
  - [ ] prune stale worktrees

  Verification:
  - [ ] `uv run pytest tests/cli/test_worktree_support.py tests/cli/test_worktree_rescue.py tests/lifecycle/test_worktree_sync.py -q`

- [ ] P9.2. Introduce `WorktreeInspector`.

  Verification:
  - [ ] read-only inspection callers route through inspector.

- [ ] P9.3. Introduce `WorktreeSyncService`.

  Verification:
  - [ ] lifecycle sync callers route through sync service.

- [ ] P9.4. Introduce `WorktreeCleanupService`.

  Verification:
  - [ ] cleanup callers route through cleanup service.

- [ ] P9.5. Introduce `WorktreeRescueService`.

  Verification:
  - [ ] rescue callers route through rescue service.

- [ ] P9.6. Keep `WorktreeService` only as a temporary facade or delete it if
  all callers use focused services.

  Verification:
  - [ ] `rg -n "WorktreeService" litehive tests`
  - [ ] `make test`

## Phase 10: RuntimeStore Internal Split

Reason: `RuntimeStore` is a persistence facade with too many table families.

- [ ] P10.1. Add characterization tests for each store family.

  Families:
  - [ ] workspace state
  - [ ] task state
  - [ ] task intent
  - [ ] process state
  - [ ] subagent counters
  - [ ] bootstrap/rebuild

  Verification:
  - [ ] `uv run pytest tests/state -q`

- [ ] P10.2. Introduce `WorkspaceStateStore`.
- [ ] P10.3. Introduce `TaskStateStore`.
- [ ] P10.4. Introduce `TaskIntentStore`.
- [ ] P10.5. Introduce `ProcessStateStore`.
- [ ] P10.6. Introduce `SubagentCounterStore`.

  Verification for each split:
  - [ ] Old `RuntimeStore` method delegates to the new internal store.
  - [ ] Focused state tests pass.
  - [ ] `make typecheck`

- [ ] P10.7. Decide whether to keep `RuntimeStore` as a facade.

  Decision rule:
  - [ ] Keep facade if it simplifies callers and does not regain policy.
  - [ ] Delete facade only if the container cleanly exposes focused stores.

## Phase 11: Subagent Session And Report Boundaries

Reason: `SubagentManager` should coordinate, not own parsing, rendering, or
persistence details.

- [ ] P11.1. Move remaining subagent artifact load/save helpers to
  `SubagentArtifactStore`.

  Verification:
  - [ ] `rg -n "load_subagent_session|load_subagent_report|load_subagent_event_stream|subagent_artifacts" litehive`

- [ ] P11.2. Introduce or complete `AgentReportService`.

  Verification:
  - [ ] `stage_report_from_subagent(...)` delegates to service or is deleted.
  - [ ] `uv run pytest tests/agents tests/lifecycle/test_engine_adapter.py -q`

- [ ] P11.3. Ensure `SubagentManager` only coordinates run sequence.

  Verification:
  - [ ] Manager no longer parses report payloads directly.
  - [ ] Manager no longer renders execution traces directly.
  - [ ] Manager persists sessions through injected session/artifact services.

## Phase 12: Daemon Object Boundary

Reason: daemon execution should be an object assembled by the daemon container.

- [ ] P12.1. Add characterization tests for daemon loop behavior.

  Verification:
  - [ ] `uv run pytest tests/daemon -q`

- [ ] P12.2. Introduce `WorkspaceDaemon`.

  Methods:
  - `run()`
  - `run_cycle()`
  - `should_continue(stop_reason)`
  - `sleep_with_stop(seconds)`
  - `maybe_backup()`
  - `stop()`

  Verification:
  - [ ] `build_daemon_container(...)` assembles daemon collaborators.
  - [ ] CLI daemon command delegates to `WorkspaceDaemon`.

- [ ] P12.3. Introduce/refine `DaemonExecution`.

  Methods:
  - `pick_next_task()`
  - `run_task(task)`
  - `handle_result(result)`
  - `record_cycle_start()`
  - `record_cycle_finish()`

  Verification:
  - [ ] daemon free functions are wrappers or removed.
  - [ ] `make test`

## Phase 13: Trim Workspace

Reason: after focused services exist, `Workspace` should stop acting as a
service locator.

- [ ] P13.1. Remove task convenience methods once callers use
  `WorkspaceTasks`.

  Verification:
  - [ ] `rg -n "\\.list_tasks\\(|\\.get_task\\(|\\.require_task\\(|\\.save_task\\(" litehive tests`

- [ ] P13.2. Remove event/activity convenience methods once callers use
  `TaskEventLog` and `TaskActivityStore`.

  Verification:
  - [ ] `rg -n "\\.append_event\\(|\\.task_activity\\(" litehive tests`

- [ ] P13.3. Remove subagent session convenience methods once callers use
  `WorkspaceSubagents` / `SubagentArtifactStore`.

  Verification:
  - [ ] `rg -n "\\.load_subagent_session" litehive tests`

- [ ] P13.4. Confirm `Workspace` only owns identity, config, paths, and DB
  connection.

  Verification:
  - [ ] Read `litehive/workspace.py` manually.
  - [ ] `make typecheck`
  - [ ] `make test`

## Final Verification

- [ ] `make typecheck`
- [ ] `make test`
- [ ] `make test-integration` if sandbox, CLI round-trips, or engine adapters
  were touched.
- [ ] `rg -n "Workspace\\.from_path\\(|root: Path" litehive` reviewed so raw
  root construction remains only at boundaries or listed debt.
- [ ] `rg -n "temporary wrapper|TODO|compat" litehive docs` reviewed so no
  migration wrapper is forgotten.
