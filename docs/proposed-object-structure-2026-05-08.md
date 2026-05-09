# Proposed Object Structure

This document describes the target object shape implied by the function
inventory in `docs/function-method-analysis-2026-05-08.md`.

It is intentionally not a generated API index. The goal is to show where
behavior should live after the migration:

- package -> class -> methods
- short class purpose
- utility functions that should stay functions, with the reason

## Principles

- `Workspace` owns workspace identity, not every workspace behavior.
- The DI container in `litehive/container.py` assembles long-lived services.
- Services receive ready collaborators in constructors.
- Pure transformations stay functions.
- Persistence classes persist; policy classes decide; coordinators orchestrate.
- Keep facades temporary and thin while call sites migrate.

## `litehive.container`

### `LitehiveContainer`

Purpose: process-level dependency graph for one workspace.

Methods:

- No behavior methods. This should remain a typed dependency bundle.

Fields/services to expose as the graph grows:

- `workspace: Workspace`
- `config: LitehiveConfig`
- `tasks: WorkspaceTasks`
- `runtime_settings: RuntimeSettingsRepository`
- `engine_routing: EngineRoutingPolicy`
- `status: StatusSnapshotCollector`
- `queue: TaskQueueService`
- `reports: TaskReportStore`
- `events: TaskEventLog`

### `PipelineContainer`

Purpose: lightweight lifecycle/pipeline persistence graph.

Methods:

- No behavior methods. It should remain a typed bundle.

Fields:

- `workspace`
- `persistence`
- `journal`

### `DaemonContainer`

Purpose: daemon-loop dependency graph.

Methods:

- No behavior methods. Runtime behavior belongs on `WorkspaceDaemon` or
  `DaemonExecution`.

Fields:

- `workspace`
- `config`
- `attention_repository`
- `daemon_execution`
- `daemon_logs`

## `litehive.workspace`

### `Workspace`

Purpose: validated workspace identity plus direct workspace infrastructure.

Keep methods:

- `from_path(root)`
- `connect(migrate=True)`
- `load_config()`
- `config()`
- `require_existing(source)`
- `create()`
- `runtime_dir()`
- `runtime_path(*parts)`
- `control_dir()`
- `control_files()`

Move out in separate tested refactor slices:

- `list_tasks(...)` -> `WorkspaceTasks.list(...)`
- `get_task(...)` -> `WorkspaceTasks.get(...)`
- `get_task_record(...)` -> `WorkspaceTasks.get_record(...)`
- `require_task(...)` -> `WorkspaceTasks.require(...)`
- `save_task(...)` -> `WorkspaceTasks.save(...)`
- `task_activity(...)` -> `TaskActivityStore.for_task(...)`
- `append_event(...)` -> `TaskEventLog.append(...)`
- `load_subagent_session(...)` -> `WorkspaceSubagents.load_session(...)`
- `load_subagent_session_record(...)` -> `WorkspaceSubagents.load_session_record(...)`
- `load_subagent_session_created_at(...)` -> `WorkspaceSubagents.load_session_created_at(...)`

Reason: `Workspace` should not become a service locator. It should answer
"where is this workspace and how do I reach its infrastructure?", not own every
task, event, and subagent operation.

Migration meaning: do not move this whole list in one broad refactor. For each
group, add or verify characterization tests, introduce the focused service,
route a small set of callers, and keep the old `Workspace` method only as a
temporary wrapper until its callers are gone.

### `WorkspaceControlFiles`

Purpose: bound paths for repo-local `.litehive` files.

Methods:

- `directory()`
- `config()`
- `context()`
- `gitignore()`

Reason: these paths are direct workspace identity/path behavior and are
appropriate as a small value object.

## `litehive.state`

### `WorkspaceTasks` or `TaskRepository`

Purpose: task record and runtime persistence API for one workspace.

Constructor dependencies:

- `workspace: Workspace`
- `runtime_store: RuntimeStore`
- `event_log: TaskEventLog`
- `audit_log: TaskAuditLog` or existing audit helper during migration

Methods:

- `create(title, goal, acceptance_criteria, constraints, plan, ...)`
- `list(include_runtime=True, strict=True)`
- `list_state_first(state=None, include_runtime=False)`
- `create_follow_ups(parent_task, stage, follow_ups)`
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

Migrates functions from:

- `litehive/state/records.py`
- task runtime helper calls that only persist a changed `TaskRecord`

### `RuntimeStore`

Purpose: low-level SQLite store facade.

Keep methods:

- `bootstrap()`
- `load_workspace_state()`
- `save_workspace_state(state)`
- `load_task_state(task_id)`
- `save_task_state(task_id, state)`
- `load_task_intent(task_id)`
- `save_task_intent(task_id, intent)`
- `delete_task(task_id)`
- `save_process_state(process_name, status, payload)`
- `clear_process_state(process_name)`
- `load_process_state(process_name)`

Split internally:

- `WorkspaceStateStore`
- `TaskStateStore`
- `TaskIntentStore`
- `ProcessStateStore`
- `SubagentCounterStore`

Reason: `RuntimeStore` is already too broad. Keep it as a facade while the
call graph is migrated, but do not add new policy or domain decisions to it.

### `WorkspaceStateRepository`

Purpose: workspace-bound policy API for queue/pool `WorkspaceState` reads and
mutations.

Constructor dependencies:

- `workspace: Workspace`
- `runtime_store: RuntimeStore`

Methods:

- `load(bootstrap=True)`
- `save(state)`
- `save_without_runner_guard(state, audit_entries=None)`
- `set_pool_stop_reason(stop_reason)`
- `record_task_completion(final_stage)`
- `merged_state_for_runner_owned_write(state, protected_task_ids=())`
- `persist_task_and_state(task, state, journal_message=None, protected_task_ids=(), audit_entries=None)`
- `persist_tasks_and_state(tasks, state, journal_messages=None, protected_task_ids=(), audit_entries=None)`
- `persist_task_and_state_without_runner_guard(task, state, journal_message=None, protected_task_ids=(), audit_entries=None)`
- `persist_tasks_and_state_without_runner_guard(tasks, state, journal_messages=None, protected_task_ids=(), audit_entries=None)`

Reason: `litehive.state.store.WorkspaceStateStore` is the low-level SQLite
adapter. `WorkspaceStateRepository` owns higher-level mutation policy such as
runner guards, bootstrap validation, and pool stop reason side effects.

### `WorkspaceRunnerLock`

Purpose: workspace-specific runner lock and reconciliation API.

Constructor dependencies:

- `workspace: Workspace`
- `process_lock: ProcessLockManager`
- `runtime_store: RuntimeStore`

Methods:

- `guard()`
- `is_active()`
- `is_held()`
- `read_metadata()`
- `write_metadata(status)`
- `clear_metadata()`
- `status()`
- `owns_current_thread()`
- `touch(active_task_id=None)`
- `heartbeat(active_task_id=None, interval_seconds=1.0)`
- `conflict_message()`
- `needs_reconciliation()`
- `pid_is_stale()`

Migrates functions from:

- `litehive/state/locking.py`

### `WorkspaceMutationGuard`

Purpose: short critical-section guard for task/workspace mutation.

Methods:

- `hold()`
- `is_owned_by_current_thread()`
- `ensure_future_task_mutation_allowed(task_ids, state=None)`
- `persist_future_task_update(task, journal_message=None, audit_entries=None)`

Reason: mutation locking is a separate responsibility from runner process
locking.

### `WorkspaceStateLock`

Purpose: short blocking workspace flock for state mutation critical sections.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `hold()`

Reason: this is the blocking `.lock` around brief workspace-state updates. It
is separate from `WorkspaceRunnerLock`, which owns long-lived runner process
identity and metadata, and separate from `WorkspaceMutationGuard`, which
decides when a caller must take the runner guard.

### `WorkspaceBackupService`

Purpose: workspace-bound SQLite database backups.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `list_backups()`
- `prune()`
- `create(when=None)`
- `create_scheduled(now=None)`
- `restore(timestamp)`

Migrates functions from:

- `litehive/state/backup.py`

### `DatabaseRebuildSafety`

Purpose: workspace-bound guardrails and backup for destructive database rebuilds.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `task_artifact_dir_ids()`
- `event_log_replay_task_ids()`
- `assert_safe(db_path, replay_task_ids=None, operation=...)`
- `backup_before_rebuild(db_path, label)`

Migrates functions from:

- `litehive/state/rebuild_safety.py`

## `litehive.config`

### `RuntimeSettingsRepository`

Purpose: audited mutable runtime settings stored in SQLite.

Constructor dependencies:

- `workspace: Workspace`
- config-data loader
- clock

Methods:

- `bootstrap(config_data=None)`
- `load()`
- `apply_to_config_data(config_data)`
- `get(key)`
- `set(key, value, actor, source, reason=None, context=None)`
- `clear(key, actor, source, reason=None, context=None)`
- `audit_entries(key=None, limit=...)`

Migrates functions from:

- `litehive/config/runtime_settings.py`

### `EngineRoutingPolicy`

Purpose: select engines and models using config, runtime settings, freezes,
quota state, and recovery policy.

Constructor dependencies:

- `config: LitehiveConfig`
- `runtime_settings: RuntimeSettingsRepository`
- `engine_monitoring: EngineMonitoringRepository`
- clock

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

Migrates functions from:

- `litehive/config/engine_models.py`
- `litehive/config/engine_freezes.py`
- `litehive/config/engine_quota.py`
- `litehive/tasks/recovery_engine.py`

### `WorkspaceConfigLoader`

Purpose: read and validate workspace/global config layers.

Methods:

- `effective_data()`
- `load()`
- `context()`
- `read_layer(path)`
- `merge_layers(base, overlay)`

Reason: loading config is boundary/persistence behavior, not `Workspace`
identity behavior.

## `litehive.tasks`

### `TaskRuntimeTransitions`

Purpose: mutate `TaskRecord.runtime` consistently and persist atomic runtime
transitions.

Constructor dependencies:

- `workspace: Workspace`
- `tasks: WorkspaceTasks`
- lock/guard collaborator
- clock

Methods:

- `start_run(task)`
- `finish_run(task, final_status)`
- `start_stage(task, stage)`
- `finish_stage(task, stage, status)`
- `add_subagent(task, subagent)`
- `mark_subagent_started(task, subagent, pid=None)`
- `mark_subagent_progress(task, subagent, execution)`
- `mark_subagent_finished(task, subagent, result)`
- `switch_engine(task, from_engine, to_engine, reason)`
- `record_outcome(task, outcome)`
- `clear_run_activity(task, execution_status)`

Migrates functions from:

- `litehive/tasks/runtime.py`

### `TaskQueueService`

Purpose: queue eligibility, selection, insertion, removal, and repair.

Constructor dependencies:

- `workspace: Workspace`
- `tasks: WorkspaceTasks`
- workspace state store

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
- `validate_dependencies(task_id, depends_on)`

Migrates functions from:

- `litehive/tasks/queue.py`
- `litehive/tasks/queue_eligibility.py`
- `litehive/tasks/queue_mutations.py`
- `litehive/tasks/queue_selection.py`

### `TaskDependencyValidator`

Purpose: workspace-bound validation for task dependency edges.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `validate(task_id, depends_on)`

Migrates functions from:

- `litehive/tasks/queue_eligibility.py`

### `TaskStatusService`

Purpose: workspace-bound owner for operator task-status transitions.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `abandon(task_id, reason, audit_actor, audit_source)`
- `close(task_id, outcome, reason, follow_up_task_id, audit_actor, audit_source)`
- `park(task_id, reason, audit_actor, audit_source)`
- `requeue(task_id, front, force, audit_actor, audit_source)`
- `resume(task_id, front)`
- `recover_completed(task_id)`
- `stop_current()`
- `switch_engine(task_id, engine, reason)`
- `update(task_id, fields..., audit_actor, audit_source)`

Migrates functions from:

- `litehive/tasks/status.py`
- `litehive/tasks/status_close.py`
- `litehive/tasks/status_resume.py`
- `litehive/tasks/status_update.py`
- `litehive/tasks/completed_task_recovery.py`
- `litehive/tasks/stop.py`
- `litehive/tasks/switch_engine.py`

### `TaskReportStore`

Purpose: persist and query stage/recovery reports.

Constructor dependencies:

- `workspace: Workspace`
- SQLite connection/runtime store

Methods:

- `record_stage_report(task, report)`
- `load_stage_reports(task, pipeline_state=None)`
- `latest_stage_report(task, pipeline_state=None)`
- `record_recovery_report(task, report)`
- `load_recovery_reports(task)`
- `latest_recovery_report(task)`

Migrates functions from:

- `litehive/tasks/report_storage.py`
- `litehive/tasks/recovery_reports.py`

### `TaskEventLog`

Purpose: durable event log and SQLite rebuild boundary.

Constructor dependencies:

- `workspace: Workspace`
- `runtime_store: RuntimeStore`

Methods:

- `append(task, event)`
- `read(task)`
- `has_events()`
- `sqlite_tables_empty()`
- `rebuild_sqlite()`
- `event_type_for_audit_action(action)`

Migrates functions from:

- `litehive/tasks/event_log.py`

### `TaskActivityStore`

Purpose: persisted task activity feed.

Methods:

- `for_task(task)`
- `record(task, entry)`
- `load(task, limit=None)`
- `latest(task)`

Migrates behavior around:

- `litehive/tasks/activity.py`
- current `Workspace.task_activity(...)` convenience method

### `TaskArtifactLocator`

Purpose: workspace-bound lookup for task and runner artifacts.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `latest_run_all_log_path()`
- `latest_subagent_base(task)`

Migrates functions from:

- `litehive/tasks/paths.py`

### `PoolService`

Purpose: runner-level queue drain attempt and summary reporting.

Constructor dependencies:

- `workspace: Workspace`
- `queue: TaskQueueService`
- `tasks: WorkspaceTasks`
- `runtime: TaskRuntimeTransitions`

Methods:

- `run(limit=None)`
- `collect_pending()`
- `collect_resumable()`
- `collect_closed()`
- `summarize(progress)`
- `render_summary(report)`
- `stop_for(reason)`

Migrates functions from:

- `litehive/cli/pool.py`
- pool report builders that currently live as free functions

## `litehive.cli`

### `TaskEvidencePresenter`

Purpose: workspace-bound presenter for `litehive task evidence` and
`litehive task debug` output.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `render_task_evidence(task)`
- `debug_all(task)`
- `debug_latest(task)`
- `debug_worktree(task)`

Migrates functions from:

- `litehive/cli/task_debug_support.py`

### `TaskLogsPresenter`

Purpose: workspace-bound presenter for `litehive task logs` output.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `show_latest_daemon_log()`
- `list_daemon_sessions()`
- `show_task_journal(task)`
- `show_latest_subagent(task)`
- `list_task_subagents(task)`
- `follow_active_subagent(task_id=None)`
- `resolve_follow_task(task_id)`
- `load_task_with_runtime(task_id)`

Migrates functions from:

- `litehive/cli/task_logs_support.py`

### `WorkspaceHealthPresenter`

Purpose: workspace-bound presenter for the `litehive health` command.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `daemon_status()`

Migrates functions from:

- `litehive/cli/workspace.py`

## `litehive.agents`

### `SubagentManager`

Purpose: coordinator for one external subagent invocation.

Keep methods:

- `run(...)`
- `_prepare_subagent_run(...)`
- `_execute_subagent_engine(...)`
- `_finalize_subagent_run(...)`

Move out in separate tested refactor slices:

- report parsing -> `AgentReportService`
- execution trace rendering -> `ExecutionTraceRenderer`
- session persistence details -> `SubagentSessionManager` /
  `SubagentArtifactStore`

### `SubagentSessionManager`

Purpose: coordinate persisted subagent session state.

Keep methods:

- `write_session_start(...)`
- `write_running_session_metadata(...)`
- `write_session_snapshot(...)`
- `write_session_finish(...)` if introduced during migration

Move/split:

- stream handling -> `SubagentStreamRecorder`
- inactivity behavior -> `SubagentInactivityMonitor`
- artifact slices -> `SubagentArtifactStore`

Migration meaning: keep `SubagentManager` and `SubagentSessionManager` working
as facades while individual responsibilities move. Each moved responsibility
needs a focused test around the old behavior before callers are rerouted.

### `SubagentArtifactStore`

Purpose: bound store for one workspace/task/subagent artifact set.

Methods:

- `save(payload)`
- `load()`
- `load_session()`
- `load_report()`
- `load_event_stream()`
- `save_event(event)`
- `save_report(report)`

### `AgentReportService`

Purpose: convert subagent activity and submitted reports into typed stage
reports and follow-up actions.

Constructor dependencies:

- `workspace: Workspace`
- `reports: TaskReportStore`
- `activity: TaskActivityStore`

Methods:

- `stage_report_from_subagent(task, stage, result)`
- `extract_verdict(activity)`
- `build_failure_diagnostics(activity)`
- `resolve_follow_up_task(request)`
- `submit(request)`

### `ExecutionTraceRenderer`

Purpose: parse/render/load human-readable execution traces.

Methods:

- `parse_unified_events(stdout)`
- `render_event(event)`
- `render_from_events(events, stderr="")`
- `render_from_streams(stdout, stderr)`
- `render_from_payload(payload, stderr="")`
- `load_for_subagent(task, ref, active=None, runtime_state=None)`

Reason: this is a coherent renderer/parser object. It can be stateless, but
grouping it gives the rendering contract one owner.

### `EngineManager`

Purpose: resolve engine adapters and resume-safe models for subagent runs.

Methods:

- `engine_for(engine_name)`
- `resume_safe_model(engine_name, model, resume_session_id)`

Do not add:

- quota/freeze/default routing policy. That belongs to `EngineRoutingPolicy`.

## `litehive.lifecycle`

### `TaskOrchestrator`

Purpose: workspace-bound entry point that wires lifecycle collaborators and
runs one task through the state machine.

Constructor dependencies:

- `workspace: Workspace`
- `config: LitehiveConfig`

Methods:

- `run(task, engine_factory=None, engine_override=None, model_override=None)`

### `StateMachineRunner`

Purpose: run the lifecycle state machine over one task.

Methods:

- existing runner methods only; do not add persistence or prompt policy.

### `SqlitePersistence`

Purpose: lifecycle state persistence.

Methods:

- `load(task_id)`
- `save(state)`
- `delete(task_id)`
- focused pipeline state persistence methods

### `SqliteJournal`

Purpose: lifecycle journal persistence and rendering input.

Methods:

- `append(...)`
- `load(...)`
- `latest(...)`

### `PipelineWorktreeSetup`

Purpose: bind one workspace to the lifecycle worktree collaborators used by
runner orchestration.

Methods:

- `resolve_worktree(state)`
- `resolve_hook_execution_root(state)`
- `task_recorded_worktree(task_id)`
- `build_commit_node()`
- `build_worktree_sync_node()`
- `worktree_missing_probe()`
- `worktree_metadata_repair()`
- `mark_task_interrupted_on_crash(task)`
- `cleanup_terminal_worktree(task)`
- `reconcile_terminal_commit_sha(task, final_state, persistence)`

### `GitCommitNode`

Purpose: state-machine node that coordinates commit-to-git behavior.

Keep methods:

- node lifecycle hooks
- orchestration of commit collaborators

Move out in separate tested refactor slices:

- dirty-check filtering -> `CommitReadinessPolicy`
- staging plan -> `GitStagePlanner`
- git commit execution -> `CommitExecutor`
- prompt/status text details -> renderer/prompt collaborator

### `CommitReadinessPolicy`

Purpose: decide whether a task checkout is ready to commit.

Methods:

- `is_ready(task, worktree)`
- `blocking_reasons(task, worktree)`
- `filter_stageable_paths(paths)`

### `GitStagePlanner`

Purpose: compute which paths should be staged for commit.

Methods:

- `stageable_paths(repo_root)`
- `ignored_or_untracked_embedded_repos(repo_root)`

### `CommitExecutor`

Purpose: execute git staging/commit operations through `litehive/git/ops.py`.

Methods:

- `stage(paths)`
- `commit(message)`
- `status_entries()`

## `litehive.worktree`

### `WorktreePaths`

Purpose: workspace-bound path policy for managed task worktrees.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `task_worktree_path(task)`
- `is_managed_worktree_path(worktree_path)`
- `resolve_recorded_worktree_path(worktree_path)`
- `ensure_venv_link(worktree_path)`

Reason: path layout, managed-path safety checks, legacy recorded-path
resolution, and the shared `.venv` link all belong to the workspace's
worktree path policy. Keeping these as methods avoids scattering
workspace-first helper functions through sync, cleanup, rescue, lifecycle, and
tests.

### `WorktreeSyncService`

Purpose: ensure task worktrees exist and point to the correct branch/ref.

Methods:

- `sync_task_worktree(task, state)`
- `registered_worktree_for_branch(branch)`
- `task_has_missing_recorded_worktree(task_id)`
- `clear_missing_recorded_worktree(task_id)`

### `TaskExecutionRootResolver`

Purpose: resolve the directory a task subagent should run in.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `resolve(task, config=None)`

Migrates functions from:

- `litehive/worktree/execution_root.py`

### `WorktreeCleanupService`

Purpose: remove terminal or stale managed worktrees when safe.

Methods:

- `collect_managed_worktrees()`
- `remove_cleanable_worktrees()`
- `cleanup_terminal_task_worktree(task)`
- `prune_stale_worktrees()`

### `WorktreeRescueService`

Purpose: find and apply rescue candidates for orphaned or broken worktrees.

Methods:

- `collect_rescue_candidates()`
- `apply_rescue_candidate(candidate)`
- `stash_local_changes(worktree)`
- `restore_local_changes(worktree, stash_ref)`

### `WorktreeInspector`

Purpose: read-only inspection of worktree status.

Methods:

- `inspect_task_worktree(task)`
- `inspect_dirty_gate()`
- `committed_changes(worktree)`
- `is_dirty(worktree)`
- `has_origin(worktree)`
- `head(worktree)`
- `unresolved(worktree)`

## `litehive.attention`

### `AttentionRepository`

Purpose: SQLite repository for append-only operator attention diagnostics.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `append(message)`
- `read(limit=None)`

### `OperatorAttentionProjector`

Purpose: workspace-bound projection and rendering of "operator needed" state.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `collect_state()`
- `waiting_lines(limit=5, reconcile=True)`

Migrates functions from:

- `litehive/attention.py`

## `litehive.recovery`

### `RunnerRecoveryService`

Purpose: workspace-bound recovery of stale runner and active-task state.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `recover_stale_runner_state(summary=None)`

Migrates functions from:

- `litehive/recovery/execution_recovery.py`

## `litehive.observability`

### `WorkspaceVenvHealth`

Purpose: workspace-bound discovery and probing of Python virtualenv entrypoints.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `discover_venvs()`
- `probe_broken_executables()`
- `ensure_ready()`

Migrates functions from:

- `litehive/observability/venv_health.py`

### `StatusSnapshotCollector`

Purpose: tolerant read-only status collection for one workspace.

Constructor dependencies:

- `workspace: Workspace`
- `config_loader: WorkspaceConfigLoader`
- `runtime_store: RuntimeStore`
- `daemon_status: DaemonStatusProbe`
- `runner_lock: WorkspaceRunnerLock`
- `engine_monitoring: EngineMonitoringRepository`

Methods:

- `collect()`
- `collect_operational()`
- `load_config()`
- `load_state()`
- `load_runner()`
- `probe_runner()`
- `probe_daemon()`
- `probe_last_cycle()`
- `probe_heru_link()`
- `probe_origin_divergence()`
- `probe_task_index_references()`

### `TaskPipelineStatusCollector`

Purpose: collect the workspace snapshot rendered by CLI status commands and
daemon status logging.

Constructor dependencies:

- `workspace: Workspace`

Methods:

- `collect(read_only=False, diagnostics=False)`
- `probe_recovery_failure()`

Migrates functions from:

- `litehive/observability/status.py`
- `litehive/observability/status_loaders.py`
- `litehive/observability/status_probes.py`
- `litehive/observability/status_health.py`
- `litehive/observability/status_diagnostics.py`

### `EngineMonitoringRepository`

Purpose: load/save engine monitoring observations.

Methods:

- `load()`
- `save(monitoring)`
- `record_usage(engine, status, observed_at)`
- `quota_state(engine)`

### `DaemonStatusProbe`

Purpose: read daemon registry/log/process state for status output.

Methods:

- `probe()`
- `last_cycle()`
- `is_alive()`

## `litehive.daemon`

### `DaemonRegistry`

Purpose: workspace-bound daemon registration, lock metadata, heartbeat, and
process-state mirror.

Methods:

- `lock_is_active()`
- `metadata()`
- `live_entry()`
- `register(pid, log_dir)`
- `unregister(pid=None)`
- `touch(pid=None)`
- `stale_metadata()`

### `WorkspaceDaemon`

Purpose: object that owns one daemon process for one workspace.

Constructor dependencies:

- `DaemonContainer`
- `DaemonExecution`
- `DaemonLogs`
- backup service
- sleeper/clock

Methods:

- `run()`
- `run_cycle()`
- `should_continue(stop_reason)`
- `sleep_with_stop(seconds)`
- `maybe_backup()`
- `stop()`

### `DaemonExecution`

Purpose: one daemon cycle's task selection and runner invocation.

Methods:

- `pick_next_task()`
- `run_task(task)`
- `handle_result(result)`
- `record_cycle_start()`
- `record_cycle_finish()`

### `DaemonStatusPresenter`

Purpose: render daemon registration, runner liveness, and latest daemon-log
paths for the operator-facing daemon status block.

Methods:

- `status_lines()`

### `DaemonStatusSnapshotCollector`

Purpose: capture read-only daemon-loop before/after task pipeline snapshots
for iteration logs and stop-reason decisions.

Methods:

- `snapshot()`

### `DaemonLogs`

Purpose: daemon log path and attention-log behavior.

Methods:

- existing methods, plus any remaining log-path helpers currently free in
  `litehive/daemon/logs.py`.

## Utility Functions

Utility functions are not "bad". They should remain functions when they do
not have identity, mutable state, injected dependencies, or a cohesive owner.

### Pure Value Transformations

Examples:

- enum canonicalization helpers
- string/slug normalization
- timestamp parsing
- small validation predicates
- conversions such as `parse_*`, `normalize_*`, `canonical_*`

Why not classes:

- A class would only wrap one stateless operation.
- There is no dependency to inject.
- Tests are clearer against a pure function.

### Low-Level Git Wrappers

Examples:

- functions in `litehive/git/ops.py`

Why not classes yet:

- The module is already the single allowed home for git subprocess calls.
- Most helpers are thin wrappers around one git command.
- A `GitRepository` class only becomes useful for multi-step workflows that
  repeatedly share one root and policy.

### CLI Boundary Functions

Examples:

- Typer command functions in `litehive/cli/*.py`

Why not classes:

- CLI functions should parse external input and dispatch.
- Business behavior should move behind services, not into command classes.
- A command class would risk hiding process-boundary concerns and making DI
  less explicit.

### Container Builders

Examples:

- `build_container(...)`
- `build_pipeline_container(...)`
- `build_daemon_container(...)`
- `build_subagent_manager(...)`

Why not classes:

- These are the composition root.
- The point is to make wiring visible in one module.
- Turning builders into methods adds another object without reducing
  complexity.

### Local Private Helpers

Examples:

- nested helper functions
- small `_helper(...)` functions used by one public operation

Why not classes:

- The owning public operation already gives them context.
- Promoting them too early increases API surface.
- They should move only when their enclosing operation moves to a service.

### Small Render Helpers

Examples:

- helpers that format one line or one field

Why not classes:

- Formatting a single value is pure transformation.
- Larger rendering pipelines should become renderer classes only when they own
  a complete output contract.

## Migration Order

1. Add `RuntimeSettingsRepository`.
2. Add `WorkspaceTasks` / `TaskRepository`.
3. Add `ExecutionTraceRenderer`.
4. Add `TaskRuntimeTransitions`.
5. Add `StatusSnapshotCollector`.
6. Keep worktree behavior split across focused services.
7. Split `RuntimeStore` internally while preserving the existing facade.
8. Trim `Workspace` convenience methods after call sites use container-owned
   services.

Each migration should preserve the old function as a wrapper until call sites
are moved, unless the call graph is already small enough to delete it in the
same tested slice.
