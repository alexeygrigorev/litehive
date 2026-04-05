# Workspace Layout

Litehive stores all workspace state in `.litehive/`. Some of that state is
durable and should be committed. Other parts are runtime-only and are ignored by
the workspace `.gitignore`.

## Top-Level Layout

Typical structure:

```text
.litehive/
  .gitignore
  config.yaml
  context.md
  state.yaml
  engine-monitoring.yaml
  pool-summary.txt
  logs/
  worktrees/
  tasks/
    T-0001-example-task/
      task.yaml
      brief.md
      runtime.yaml
      journal.md
      thread.yaml
      events.jsonl
      reports/
      recovery/
      subagents/
```

## Tracked Versus Ignored

The workspace `.gitignore` ignores these paths:

```text
.lock
.runner.lock
logs/
pool-summary.txt
engine-monitoring.yaml
worktrees/
tasks/*/runtime.yaml
tasks/*/reports/commit_to_git-*.yaml
```

That means the main tracked Litehive files are:

- `.litehive/.gitignore`
- `.litehive/config.yaml`
- `.litehive/context.md`
- `.litehive/state.yaml`
- each task directory under `.litehive/tasks/<task-id>-<slug>/`
- task `task.yaml`
- task `brief.md`
- most stage reports under `reports/`
- task `journal.md`
- task `thread.yaml`
- task `events.jsonl`
- recovery reports under `recovery/`

The main ignored runtime files are:

- `.litehive/logs/`
- `.litehive/worktrees/`
- `.litehive/pool-summary.txt`
- `.litehive/engine-monitoring.yaml`
- task `runtime.yaml`
- `commit_to_git-*` stage reports

## Why This Split Exists

Litehive tries to keep the durable audit trail in git while leaving high-churn
runtime state untracked.

Tracked artifacts answer:

- what task exists?
- what was the plan and acceptance target?
- what did each stage report?
- what recovery evidence and thread history were recorded?

Ignored artifacts answer:

- what is happening right now?
- where is the active worktree?
- what did the latest daemon run print?
- what temporary runtime or commit-integration state exists?

## Key Files

### `.litehive/config.yaml`

Workspace-local configuration. This is part of the repository and is intended to
be shared.

### `.litehive/context.md`

Project context and process overlay used to build agent prompts. Also intended to
be shared.

### `.litehive/state.yaml`

Workspace queue state, including:

- active task id
- queue order
- current workspace mode
- pool stop reason
- next task number

### Task `task.yaml`

The durable task record. This is the primary source of truth for:

- title
- goal
- acceptance criteria
- constraints
- plan
- task type
- retry policy
- pipeline status
- terminal outcome metadata

### Task `runtime.yaml`

Live runtime state such as continuation handoff and active worktree metadata.
Ignored because it is volatile.

### Task `journal.md`

Human-readable task history and lifecycle notes.

### Task `thread.yaml`

Structured comments and reports written by agents or operator actions.

### Task `events.jsonl`

Live event stream captured for execution visibility and later diagnosis.

### Task `reports/`

Stage verdict records. These are generally tracked, except the
`commit_to_git-*.yaml` reports, which are ignored.

### Task `recovery/`

Recovery reports and related evidence summaries.

### Task `subagents/`

Subagent execution artifacts, including `session.yaml` and `report.yaml` plus
transcripts and other raw data when retained.

## Worktrees

When Litehive runs tasks in isolated worktrees, those live under:

```text
.litehive/worktrees/
```

They are intentionally ignored because they are ephemeral execution sandboxes,
not durable project state.

## Logs

Background and pool-run logs live under:

```text
.litehive/logs/
```

Notable examples:

- daemon run-all logs
- pool-run summaries
- other operator-facing diagnostics

These are useful for debugging but are not intended to create permanent repo
churn.

## Commit Behavior

During `commit_to_git`, Litehive treats these `.litehive/` files as committable
workspace metadata:

- `.litehive/.gitignore`
- `.litehive/config.yaml`
- `.litehive/context.md`
- `.litehive/state.yaml`

If only ignored runtime files changed, Litehive can skip creating a checkpoint
commit and mark the task done without adding meaningless git noise.

## What To Read First When Debugging

For a specific task, the highest-signal files are usually:

1. `task.yaml`
2. latest file in `reports/`
3. latest file in `recovery/`, if present
4. `thread.yaml`
5. `journal.md`
6. `runtime.yaml` for current live state

That order matches Litehive's intended operator and agent workflow.
