# Workspace Layout

Litehive splits state into two surfaces:

- repo-local `.litehive/` for durable, shareable workspace intent
- global `${LITEHIVE_HOME:-~/.local/share/litehive}/` for user-local runtime state

The repo-local surface is designed for git. The global surface is designed for
operations: one place to inspect logs, worktrees, daemon state, and SQLite
databases.

## Repo-Local Layout

Typical committed workspace structure:

```text
.litehive/
  .gitignore
  config.yaml
  context.md
  engine-monitoring.yaml
  pool-summary.txt
  tasks/
    T-0001-example-task/
      task.yaml
      brief.md
      journal.md
      comments.yaml
      events.jsonl
      reports/
      recovery/
      subagents/
```

## Global Runtime Root

User-global runtime state lives under:

```text
~/.local/share/litehive/
  config.yaml
  litehive.db
  <workspace_id>/
    data.db
    backups/
    logs/
      run-all/
    runtime/
      .daemon.lock
      .runner.lock
    subagents/
    worktrees/
```

`LITEHIVE_HOME` overrides that root for tests and alternate installs.

## Tracked Versus Ignored

The workspace `.gitignore` ignores these repo-local paths:

```text
.lock
pool-summary.txt
engine-monitoring.yaml
tasks/*/reports/commit_to_git-*.yaml
```

That means the main tracked Litehive files are:

- `.litehive/.gitignore`
- `.litehive/config.yaml`
- `.litehive/context.md`
- each task directory under `.litehive/tasks/<task-id>-<slug>/`
- task `task.yaml`
- task `brief.md`
- most stage reports under `reports/`
- task `journal.md`
- task `comments.yaml`
- task `events.jsonl`
- recovery reports under `recovery/`

The main ignored or global-runtime files are:

- `.litehive/pool-summary.txt`
- `.litehive/engine-monitoring.yaml`
- `commit_to_git-*` stage reports
- `${LITEHIVE_HOME:-~/.local/share/litehive}/config.yaml`
- `${LITEHIVE_HOME:-~/.local/share/litehive}/litehive.db`
- `${LITEHIVE_HOME:-~/.local/share/litehive}/<workspace_id>/runtime/`
- `${LITEHIVE_HOME:-~/.local/share/litehive}/<workspace_id>/logs/`
- `${LITEHIVE_HOME:-~/.local/share/litehive}/<workspace_id>/worktrees/`

## Why This Split Exists

Litehive keeps the durable audit trail in git while leaving high-churn runtime
state in a single inspectable global root.

Tracked artifacts answer:

- what task exists?
- what was the plan and acceptance target?
- what did each stage report?
- what recovery evidence and comment history were recorded?

Ignored or global-runtime artifacts answer:

- what is happening right now?
- where is the active worktree under the global runtime root?
- what did the latest daemon run print?
- what temporary runtime or commit-integration state exists?

## Key Files

### `.litehive/config.yaml`

Workspace-local configuration. This is part of the repository and is intended to
be shared.

### `.litehive/context.md`

Project context and process overlay used to build agent prompts. Also intended to
be shared.

### `${LITEHIVE_HOME}/config.yaml`

Global user defaults applied before workspace-local config.

### `${LITEHIVE_HOME}/litehive.db`

Cross-workspace SQLite registry.

### `${LITEHIVE_HOME}/<workspace_id>/data.db`

Workspace runtime SQLite database, including queue state and task runtime state.

### `${LITEHIVE_HOME}/<workspace_id>/runtime/`

Workspace-local lock metadata for the current daemon and runner processes.

### `${LITEHIVE_HOME}/<workspace_id>/worktrees/`

Ephemeral task worktrees for isolated execution.

### `${LITEHIVE_HOME}/<workspace_id>/logs/run-all/`

Daemon and pool-run logs for operator debugging.

## Commit Behavior

During `commit_to_git`, Litehive treats these `.litehive/` files as committable
workspace metadata:

- `.litehive/.gitignore`
- `.litehive/config.yaml`
- `.litehive/context.md`

If only ignored runtime files changed, Litehive can skip creating a checkpoint
commit and mark the task done without adding meaningless git noise.

## What To Read First When Debugging

For a specific task, the highest-signal files are usually:

1. `task.yaml`
2. latest file in `reports/`
3. latest file in `recovery/`, if present
4. `comments.yaml`
5. `journal.md`
6. `${LITEHIVE_HOME}/<workspace_id>/data.db` for current live state
7. `${LITEHIVE_HOME}/<workspace_id>/logs/run-all/` for daemon execution output

That order matches Litehive's intended operator and agent workflow.
