# Refactor Todo 2026-05-07

Active goal: keep burning down internal `root: Path` and path-based factory wrappers while preserving green typecheck/tests after each small commit.

## Current Focus

- [x] Finish `state.records` workspace migration:
  - [x] Add workspace-native helper path for task-number reservation.
  - [x] Add workspace-native helper path for created-task persistence.
  - [x] Switch `create_task_for_workspace` to the workspace-native helpers.
  - [x] Switch creation/follow-up/list ordering helpers to workspace state loaders.
  - [x] Switch guarded/runtime task writes to workspace-native helpers.
  - [x] Keep path wrappers only where external/path-based callers still require them.
- [x] Reduce remaining `runtime_store(root)` production calls:
  - [x] `state.records.py`
  - [x] `state.locking.py`
  - [x] `config/workspace.py`
- [x] Revisit `runtime_store(root)` factory once production callers are gone.

## Next Queues

- [x] Migrate `state.persist` path compatibility wrappers where callers already hold `Workspace`:
  - [x] Pool stop/completion helpers used by runner and daemon.
  - [x] Remaining load/save wrappers in CLI/task/recovery callers.
- [x] Move agent task mutation target from raw root to `Workspace`.
- [x] Add workspace-native daemon registry helpers for daemon execution/status paths.
- [x] Move `WorktreeService` task reads/writes to its injected `Workspace`.
- [x] Move `SubagentManager` task saves to its injected `Workspace`.
- [x] Remove duplicate raw root constructor argument from `SubagentManager`.
- [x] Remove duplicate raw root field from `SubagentSessionManager`.
- [x] Move `DockerSandboxLauncher` constructor from raw root to injected `Workspace`.
- [x] Remove duplicate raw workspace root constructor argument from `HeruEngineAdapter`.
- [x] Move `SubprocessHookRunner` constructor from raw root to injected `Workspace`.
- [x] Remove duplicate cached root field from `SubprocessHookRunner`.
- [x] Move `GitCommitNode` constructor from raw root to injected `Workspace`.
- [x] Remove duplicate cached root field from `GitWorktreeSyncNode`.
- [x] Move Heru execution-root helpers from raw root to injected `Workspace`.
- [x] Remove raw-root alternate `SubagentManager` construction path from the DI container.
- [x] Move recovery scope-analysis internals from raw root to injected `Workspace`.
- [x] Move daemon termination helpers from raw root to injected `Workspace`.
- [x] Move workspace health daemon-status lookup to workspace-native registry helper.
- [x] Move daemon start/stop internals to workspace-native registry helpers.
- [x] Move backup restore daemon check to workspace-native registry helper.
- [x] Add workspace-native stale daemon metadata helper.
- [x] Migrate `config.loading.load_config(root)` callers that already hold `Workspace`.
- [x] Add a production guardrail against raw workspace-root constructor regressions.
- [x] Remove dead raw-root parameter from recovery skip scan helper.
- [x] Remove duplicate cached root field from `WorktreeService`.
- [x] Remove duplicate cached root field from `DockerSandboxLauncher`.
- [x] Remove duplicate cached root field from `RuntimeStore`.
- [x] Remove duplicate cached root field from `SubagentManager`.
- [x] Extend the root-constructor guardrail to block cached `workspace.root` fields.
- [x] Move `WorktreeService` committed-change inspection to a workspace-native helper.
- [x] Add workspace-native backup helpers and migrate CLI/daemon callers.
- [x] Add workspace-native rebuild-safety helpers and migrate event-log replay.
- [x] Add workspace-native runner-lock probes and migrate recovery callers.
- [x] Migrate stop/close task flows to workspace-native runner-lock probes.
- [x] Add workspace-native workspace lock helper and migrate repair/resume callers.
- [x] Migrate runtime/completed-task transitions to workspace-native workspace lock.
- [x] Migrate status-update and stale-runner recovery to workspace-native workspace lock.
- [x] Migrate stop/close task transitions to workspace-native workspace lock.
- [x] Migrate queue mutation/selection flows to workspace-native workspace lock.
- [x] Move status-update runner ownership check to workspace-native lock helper.
- [x] Add workspace-native runner heartbeat helper and migrate lifecycle orchestration.
- [x] Add workspace-native worktree path helpers and migrate execution-root setup.
- [x] Migrate hook worktree setup to workspace-native recorded-path helper.
- [x] Migrate dirty-worktree inspection to workspace-native recorded-path helper.
- [x] Migrate `WorktreeService` path resolution and venv linking to workspace-native helpers.
- [x] Migrate task requeue checkout resolution to workspace-native recorded-path helper.
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
