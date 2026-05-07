# Refactor Todo 2026-05-07

Active goal: keep burning down internal `root: Path` and path-based factory wrappers while preserving green typecheck/tests after each small commit.

## Current Focus

- [ ] Finish `state.records` workspace migration:
  - [x] Add workspace-native helper path for task-number reservation.
  - [x] Add workspace-native helper path for created-task persistence.
  - [x] Switch `create_task_for_workspace` to the workspace-native helpers.
  - [ ] Keep path wrappers only where external/path-based callers still require them.
- [ ] Reduce remaining `runtime_store(root)` production calls:
  - [x] `state.records.py`
  - [ ] `state.locking.py`
  - [ ] `config/workspace.py`
- [ ] Revisit `runtime_store(root)` factory once production callers are gone.

## Next Queues

- [ ] Migrate `state.persist` path compatibility wrappers where callers already hold `Workspace`.
- [ ] Migrate `config.loading.load_config(root)` callers that already hold `Workspace`.
- [ ] Leave true boundary/path modules alone unless a clear service boundary exists:
  - `git/ops.py`
  - `config/paths.py`
  - `config/workspace_files.py`
  - `tasks/paths.py`
  - `db/schema.py`

## Validation Rule

- [ ] Run focused lint/tests for each slice.
- [ ] Run `make typecheck`.
- [ ] Run `make test`.
- [ ] Commit each green slice before moving on.
