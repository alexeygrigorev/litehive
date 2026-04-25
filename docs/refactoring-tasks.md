# Refactoring Tasks

This file is the stable refactoring queue for the `docs/domain.md` alignment
work. It exists separately from the main refactor plan so the task list does
not get lost if the larger plan is rewritten during implementation.

Source documents:

- `docs/domain.md`
- `docs/plans/code-fixes-plan.md`

Queue policy:

- all refactoring tasks created from this file should stay at the front of the
  queue until the domain/storage cleanup is complete
- tasks should be shipped in small, verifiable slices
- avoid mixing vocabulary changes, storage migration, and behavior changes in
  the same task unless the boundary is too coupled to split safely

## Current inventory summary

Already done:

- no active structured subagent `session.yaml` / `report.yaml` /
  event-stream YAML artifacts
- recovery/state compatibility cleanup already landed
- schema reset to one baseline migration already landed

Still active:

- `task.yaml` is still active structured task storage
- activity uses mixed old/new implementation:
  - SQLite table exists
  - code still uses `TaskThreadComment` and thread/comment naming
- recovery reports still persist as YAML
- `engine-monitoring.yaml` still exists
- task status semantics still use legacy closed/merge statuses
- pipeline/runtime/recovery naming still diverges from `docs/domain.md`

## Refactoring queue

### 1. Introduce an activity service boundary

Litehive task id: `T-0377`

Goal:

- create one activity-oriented read/write boundary that callers can use before
  the deeper rename and storage cleanup

Scope:

- centralize task activity access behind service/store functions
- keep behavior the same for now
- do not rename the domain types yet

Acceptance criteria:

- prompt serialization reads task history through the activity boundary
- report submission writes through the activity boundary
- debug/report helpers no longer reach into ad hoc thread storage directly

Suggested validation:

- `tests/lifecycle/test_prompt_serializer.py`
- `tests/cli/test_agent_report.py`
- `tests/cli/test_task_debug.py`

### 2. Rename thread/comment vocabulary to activity vocabulary

Litehive task id: `T-0378`

Goal:

- replace the old “thread/comments” naming with the selected activity names

Scope:

- `TaskThreadComment` -> `ActivityEntry`
- `load_task_thread` -> `load_task_activity`
- `save_task_thread` -> `save_task_activity`
- `append_thread_comment` -> `append_activity_entry`
- replace user-facing “discussion thread” text where it means task activity

Acceptance criteria:

- no active code references `TaskThreadComment`
- no active code uses thread/comment helper names
- prompt/CLI output uses activity terminology

Suggested validation:

- `tests/tasks/test_comments.py`
- `tests/lifecycle/test_prompt_serializer.py`
- `tests/cli/test_agent_report.py`

### 3. Delete `comments.yaml` and finish SQLite-backed task activity

Litehive task id: `T-0379`

Goal:

- make task activity fully SQLite-backed and remove the filesystem fallback

Scope:

- finish moving task activity persistence to `task_activity`
- remove `comments.yaml` readers/writers
- remove corrupt-YAML recovery paths for task activity

Acceptance criteria:

- no active code reads or writes `comments.yaml`
- recent task activity still appears in prompts, logs, and CLI views

Suggested validation:

- `tests/tasks/test_comments.py`
- `tests/lifecycle/test_prompt_serializer.py`
- `tests/cli/test_agent_report.py`
- `tests/cli/test_task_debug.py`

### 4. Align `StageReport` with the canonical report model

Litehive task id: `T-0380`

Goal:

- make `StageReport` match the chosen `docs/domain.md` shape

Scope:

- `stage` -> `pipeline_state`
- narrow verdict semantics so comments are not treated as stage verdicts
- remove `files_changed` from the canonical report structure

Acceptance criteria:

- `StageReport` uses canonical names
- routing/reporting still works with the updated report shape

Suggested validation:

- `tests/tasks/test_runtime_updates.py`
- `tests/lifecycle/test_engine_adapter.py`
- `tests/cli/test_agent_report.py`

### 5. Move stage and recovery reports off YAML

Litehive task id: `T-0381`

Goal:

- remove structured report YAML from the active system

Scope:

- move stage reports to SQLite-backed storage
- move recovery reports to SQLite-backed storage
- remove `reports/*.yaml` and `recovery-*.yaml` as active structured storage

Acceptance criteria:

- no active code writes structured stage/recovery YAML
- debug and recovery commands still show the latest report context

Suggested validation:

- `tests/recovery/test_runner_recovery.py`
- `tests/cli/test_task_debug.py`
- `tests/cli/test_logs.py`

### 6. Remove active `task.yaml` usage

Litehive task id: `T-0382`

Goal:

- move incomplete-task durability fully into SQLite

Scope:

- stop reading incomplete tasks from filesystem task files
- stop writing incomplete-task state to `task.yaml`
- keep filesystem task directories only for unstructured artifacts if needed

Acceptance criteria:

- active queue/load/bootstrap paths do not depend on `task.yaml`
- `.litehive/config.yaml` is the only remaining Litehive-owned YAML file

Suggested validation:

- `tests/state/test_task_runtime_storage.py`
- `tests/config/test_workspace_bootstrap.py`
- `tests/tasks/test_create_task.py`

### 7. Collapse terminal task statuses to `done` / `flagged` / `closed`

Litehive task id: `T-0383`

Goal:

- make task closing semantics match `docs/domain.md`

Scope:

- add `close_reason`
- keep merge/operator-attention cases under `flagged` with `flag_reason`
- remove ad hoc terminal statuses:
  - `merge_failed`
  - `cancelled`
  - `wont_do`
  - `deferred`
  - `duplicate`

Acceptance criteria:

- task state persists canonical terminal statuses only
- CLI/reporting surfaces use `close_reason` and `flag_reason`

Suggested validation:

- `tests/tasks/test_status_updates.py`
- `tests/tasks/test_close_active.py`
- `tests/observability/test_attention_queue.py`

### 8. Introduce the real canonical `PipelineState`

Litehive task id: `T-0384`

Goal:

- stop aliasing `PipelineState` to the coarse pipeline-status enum

Scope:

- define the real machine-state enum in the domain layer
- separate it from user-facing stage/status views
- move current lifecycle holders onto the canonical type

Acceptance criteria:

- prompts, persistence, and transition logic agree on one `PipelineState`
- no `PipelineState`-to-`PipelineStatus` alias remains

Suggested validation:

- `tests/lifecycle/test_transitions.py`
- `tests/lifecycle/test_journal_cli.py`
- `tests/lifecycle/test_sqlite_adapters.py`

### 9. Split `TaskRuntime` into pipeline and execution slices

Litehive task id: `T-0385`

Goal:

- match the target `TaskRuntime.pipeline` / `TaskRuntime.execution` design

Scope:

- introduce `PipelineRuntime`
- introduce `ExecutionRuntime`
- move flat runtime fields into the owning slice

Acceptance criteria:

- no mixed-concern flat `TaskRuntime` bucket remains
- subagent execution and retry state still persist correctly

Suggested validation:

- `tests/state/test_task_runtime_storage.py`
- `tests/tasks/test_runtime_updates.py`
- `tests/recovery/test_runner_recovery.py`

### 10. Reconcile recovery naming with the chosen domain model

Litehive task id: `T-0386`

Goal:

- align recovery terms and models across code and docs

Scope:

- use the implemented recovery vocabulary consistently:
  - `FailureFingerprint` for recovery identity and budget tracking
  - report `failure_diagnostics` for report/outcome evidence only
  - `RecoveryTrigger` / `recovery_trigger` for active recovery context
  - `RecoveryOutcome` / `recovery_history` for completed recovery attempts
  - `RuntimeRecoveryOutcome` for the compact runtime projection
- document that the retired names `FailureDiagnostics`, `RecoveryRecord`, and
  `RecoveryContext` are not domain models
- update prompts, persistence, and transition logic consistently

Acceptance criteria:

- one recovery vocabulary is used across domain models, persistence, prompts,
  and recovery logic

Suggested validation:

- `tests/lifecycle/test_prompt_serializer.py`
- `tests/lifecycle/test_transitions.py`
- `tests/recovery/test_runner_recovery.py`

### 11. Remove workspace-level structured YAML outside config

Litehive task id: `T-0387`

Goal:

- finish the workspace-level YAML cleanup

Scope:

- remove `engine-monitoring.yaml`
- remove pool summary YAML
- migrate commands to the new persistence source or delete redundant state

Acceptance criteria:

- no active code writes workspace-level structured YAML outside config
- status/diagnostic commands still render the same operator-facing data

Suggested validation:

- `tests/observability/test_status_diagnostics.py`
- `tests/observability/test_task_summary.py`
- `tests/cli/test_workspace_health.py`

### 12. Final artifact terminology cleanup

Litehive task id: `T-0388`

Goal:

- align remaining artifact terms with the chosen vocabulary

Scope:

- use `event stream` for structured subagent event data
- use `execution trace` for rendered structured subagent output where appropriate
- keep `journal` distinct from task activity

Acceptance criteria:

- code and docs use one artifact vocabulary
- plain-text artifacts remain only where they are intentionally unstructured

Suggested validation:

- targeted prompt/debug/log tests covering the renamed artifacts
