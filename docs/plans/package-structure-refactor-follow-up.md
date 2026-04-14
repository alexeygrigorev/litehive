# Litehive refactor follow-up

This document covers only the work that still needs to be done after the package-structure refactor.

Already done:

- old package imports were removed
- `pipeline/`, `workspace/`, `models/`, `storage/`, and the old `tasks/*` split were removed
- the regular `tests/` suite is green after those deletions

Still pending:

1. remove remaining legacy data compatibility code
2. finish the final `config/` package slimdown

## Scope

This follow-up is intentionally aggressive:

- no silent migration
- no fallback reads from legacy locations
- no repair-time conversion of old files
- old layouts should fail clearly instead of being auto-imported

Assumption: active workspaces already use the current unified data root, SQLite-backed runtime state, and `comments.yaml`.

If that assumption is false, stop and do a one-shot offline migration before landing these deletions.

## Remaining work

### 1. Remove legacy global/root path migration

Files:

- `litehive/config/paths.py`
- `litehive/config/workspace.py`
- `litehive/daemon/logs.py`
- `tests/test_workspace_bootstrap.py`
- `tests/test_status_broken_states.py`

Delete:

- `_migrate_legacy_layout()`
- `migrate_legacy_workspace_state()`
- `legacy_*` path helpers that only exist for migration
- bootstrap-time legacy import in `ensure_workspace()`
- log-path legacy import in `latest_run_all_log_dir()`

Rewrite or delete tests that assert legacy XDG or repo-local runtime import.

### 2. Remove legacy workspace registry YAML import

Files:

- `litehive/config/registry.py`
- `litehive/observability/status_diagnostics.py`
- `tests/test_workspace_bootstrap.py`
- `tests/test_status_broken_states.py`

Delete:

- `_migrate_legacy_yaml()`
- `workspaces.yaml` import behavior
- status checks that probe legacy registry YAML files

Keep diagnostics only for current registry surfaces.

### 3. Make task-state loading strict

Files:

- `litehive/state/records.py`
- `tests/test_workspace_bootstrap.py`
- `tests/test_task_engine_cleanup.py`

Chosen failure mode:

- **Option A**
- fail loudly and immediately when a task depends on legacy runtime files
- raise a dedicated exception such as `LegacyTaskStateError`

Delete:

- `_drop_legacy_task_engine_field()`
- `_backfill_legacy_task_state()`
- `runtime.yaml` fallback in `_load_task_runtime()`

Rewrite:

- `load_task_record_file()` to validate only the current task YAML contract

Delete or rewrite tests that assert legacy task/runtime backfill.

### 4. Remove legacy thread-file support

Files:

- `litehive/tasks/paths.py`
- `litehive/tasks/reports.py`
- `litehive/recovery/workspace_repair.py`
- `litehive/roles/recovery.py`
- `litehive/lifecycle/prompt_serializer.py`
- `tests/test_task_comments.py`

Delete:

- `legacy_task_thread_file()`
- `thread.yaml` fallback in `load_task_thread()`
- repair-time migration of `thread.yaml` into `comments.yaml`

Keep:

- `comments.yaml` as the only task discussion file

### 5. Clean up diagnostics and wording

Files:

- `litehive/observability/status_diagnostics.py`
- `litehive/config/paths.py`
- `litehive/tasks/reports.py`
- `litehive/roles/recovery.py`

Update:

- remediation text
- status output
- recovery prompt text

Remove wording that presents legacy files as supported compatibility sources.

### 6. Finish config slimdown

Current `config/` is smaller than before, but not at the target shape yet.

Still pending:

- merge `config/normalization.py` into `config/loading.py` or otherwise eliminate it as a separate layer
- move any remaining dataclass/config-only types into the intended final home
- confirm the package reaches the final reduced shape described in the original refactor plan

## Test order

Run these after each stage:

1. `uv run pytest tests/test_workspace_bootstrap.py -q`
2. `uv run pytest tests/test_status_broken_states.py -q`
3. `uv run pytest tests/test_task_engine_cleanup.py -q`
4. `uv run pytest tests/test_task_comments.py -q`
5. `uv run pytest tests -q`

Do not use `tests_integration/` as the gating signal for this follow-up.

## Success criteria

This follow-up is done when:

1. no production code imports or reads from legacy global/runtime/task paths
2. `litehive status` no longer reports on legacy registry files
3. task comments only use `comments.yaml`
4. task runtime only uses SQLite-backed task state
5. `config/` reaches its intended final reduced shape
6. `uv run pytest tests -q` stays green
