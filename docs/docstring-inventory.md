# Docstring Inventory

Tracking file for adding comprehensive docstrings across the Litehive codebase.

## Conventions

- Every class needs a docstring describing its purpose and role.
- Every dataclass/Pydantic model field needs an inline comment or
  attribute-level docstring explaining what the field stores and why.
- Every public method needs a docstring describing what it does, when it is
  called, and what its parameters mean.
- Parameters whose domain meaning is not obvious from the type and name need
  explicit documentation.
- Docstrings should not be trivial (not just restating the function name).
- Follow the project's docstring style from `docs/code-style.md`.

## Status Legend

- [ ] Not started
- [x] Complete

---

## domain/ (15 files)

- [x] agent.py
- [x] common.py
- [x] engine.py
- [x] failure_diagnostics.py
- [x] lifecycle_deltas.py
- [x] outcomes.py
- [x] pool.py
- [x] recovery.py
- [x] reports.py
- [x] roles.py
- [x] runtime.py
- [x] task.py
- [x] task_ops.py
- [x] worktree.py

## config/ (17 files)

- [x] engine_freezes.py
- [x] engine_models.py
- [x] engine_quota.py
- [x] environment.py
- [x] loading.py
- [x] model.py
- [x] paths.py
- [x] runtime_settings.py
- [x] time_parsing.py
- [x] workspace.py
- [x] workspace_files.py
- [x] profiles/defaults.py
- [x] profiles/loader.py
- [x] profiles/model.py
- [x] profiles/rendering.py

## agents/ (21 files)

- [x] artifacts.py
- [x] callbacks.py
- [x] command_policy.py
- [x] engine_callables.py
- [x] engine_manager.py
- [x] execution_trace.py
- [x] manager.py
- [x] merge_resolver.py
- [x] report_extraction.py
- [x] report_submission.py
- [x] session.py
- [x] session_continuation.py
- [x] session_events.py
- [x] session_inactivity.py
- [x] session_reports.py
- [x] session_snapshots.py
- [x] session_store.py
- [x] session_streams.py
- [x] subagent_ids.py
- [x] task_mutation.py

## lifecycle/ (28 files)

- [x] engines.py
- [x] events.py
- [x] guards.py
- [x] heru_factory.py
- [x] hook_reports.py
- [x] journal.py
- [x] launch_state.py
- [x] nodes/agent.py
- [x] nodes/base.py
- [x] nodes/hook.py
- [x] nodes/system.py
- [x] nodes/terminal.py
- [x] orchestration.py
- [x] persistence.py
- [x] prompt_sections.py
- [x] prompt_serializer.py
- [x] prompt_types.py
- [x] registry.py
- [x] rules.py
- [x] runner.py
- [x] runtime_sync.py
- [x] sessions.py
- [x] stages.py
- [x] transitions.py
- [x] types.py
- [x] worktree_setup.py

## state/ (9 files)

- [x] backup.py
- [x] lock_manager.py
- [x] locking.py
- [x] persist.py
- [x] process_lock.py
- [x] rebuild_safety.py
- [x] records.py
- [x] store.py

## tasks/ (28 files)

- [x] _process_signals.py
- [x] _status_helpers.py
- [x] activity.py
- [x] activity_rendering.py
- [x] audit.py
- [x] completed_task_recovery.py
- [x] constants.py
- [x] event_log.py
- [x] failed_runs.py
- [x] journal.py
- [x] normalization.py
- [x] paths.py
- [x] queue.py
- [x] queue_eligibility.py
- [x] queue_mutations.py
- [x] queue_selection.py
- [x] recovery_engine.py
- [x] recovery_evidence.py
- [x] recovery_reports.py
- [x] report_storage.py
- [x] runtime.py
- [x] status.py
- [x] status_close.py
- [x] status_resume.py
- [x] status_update.py
- [x] stop.py
- [x] switch_engine.py

## cli/ (18 files)

- [x] agent_cli.py
- [x] agent_dispatch.py
- [x] app.py
- [x] common.py
- [x] daemon_cli.py
- [x] display.py
- [x] engine.py
- [x] parse.py
- [x] pipeline_cli.py
- [x] pool.py
- [x] queue_cli.py
- [x] runner.py
- [x] task_cli.py
- [x] task_debug_support.py
- [x] task_logs_support.py
- [x] workspace.py
- [x] worktree_cli.py

## recovery/ (9 files)

- [x] detection.py
- [x] execution_recovery.py
- [x] interrupted_subagent.py
- [x] interruption_state.py
- [x] nonrunning_resumable_repair.py
- [x] running_task_recovery.py
- [x] scope_analysis.py
- [x] workspace_repair.py

## worktree/ (7 files)

- [x] cleanup.py
- [x] execution_root.py
- [x] inspection.py
- [x] paths.py
- [x] rescue.py
- [x] sync.py

## daemon/ (6 files)

- [x] execution.py
- [x] logs.py
- [x] registry.py
- [x] task_execution.py
- [x] termination.py

## observability/ (14 files)

- [x] engine_monitoring.py
- [x] events.py
- [x] status.py
- [x] status_dashboard.py
- [x] status_diagnostics.py
- [x] status_health.py
- [x] status_io.py
- [x] status_loaders.py
- [x] status_probes.py
- [x] status_rendering.py
- [x] status_summary.py
- [x] status_types.py
- [x] venv_health.py

## sandbox/ (5 files)

- [x] adapter.py
- [x] git_wrapper.py
- [x] launcher.py
- [x] support.py

## roles/ (9 files)

- [x] base.py
- [x] guidance.py
- [x] merge.py
- [x] planner.py
- [x] qa.py
- [x] recovery.py
- [x] reviewer.py
- [x] swe.py

## git/ (2 files)

- [x] ops.py

## db/ (5 files)

- [x] schema.py
- [x] migration_hooks/task_intent_columns.py

## Root modules (6 files)

- [x] attention.py
- [x] container.py
- [x] feedback.py
- [x] fs_cleanup.py
- [x] main.py
- [x] workspace.py
