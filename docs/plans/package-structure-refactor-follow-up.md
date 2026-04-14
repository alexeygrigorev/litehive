# Litehive refactor follow-up

Status: completed.

This document is kept as a historical record of the final cleanup that landed
after the package-structure refactor. It is no longer an active to-do list.

## Landed outcomes

- Legacy global/runtime path migration code was removed.
- Legacy workspace-registry YAML import behavior was removed.
- Task runtime loading is strict:
  - task runtime lives in SQLite
  - removed task/runtime fields fail loudly
  - `runtime.yaml` is not used as a fallback
- Task discussion is strict:
  - `comments.yaml` is the only supported discussion file
  - `thread.yaml` is not migrated or read
- Repo-local workspace state no longer uses `.litehive/state.yaml`.
- The global daemon registry no longer uses `daemons.yaml`.
- Runner and daemon PID metadata now live under the unified per-workspace
  runtime root:
  - `${LITEHIVE_HOME}/<workspace_id>/runtime/.runner.lock`
  - `${LITEHIVE_HOME}/<workspace_id>/runtime/.daemon.lock`
- The `config/` package was reduced to the current smaller shape, and config
  loading now focuses on current supported keys rather than compatibility
  shims.

## Current source of truth

For the current runtime layout and supported files, use:

- `docs/workspace-layout.md`
- `docs/configuration.md`
- `docs/recovery.md`
- `docs/code-style.md`

## Validation

The cleanup was validated with focused tests around:

- workspace bootstrap
- status diagnostics
- task runtime storage and strict task loading
- task comments
- daemon/backup behavior

Future work should be tracked in a new plan doc rather than reopening this one.
