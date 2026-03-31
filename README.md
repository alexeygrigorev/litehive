# litehive

Local-first autonomous coding workspace with deterministic task execution.

## v1 goals

- Single active task at a time
- Local YAML-backed workspace state
- Two modes: `tasks` and `implementation`
- Deterministic stage pipeline
- Subagents executed through external CLIs (`codex` and `opencode` first)
- Checkpoint commit after successful task completion

## Planned commands

- `litehive configure`
- `litehive`
- `litehive tasks`
- `litehive add`
- `litehive status`

## Workspace shape

```text
.litehive/
  config.yaml
  context.md
  state.yaml
  tasks/
    T-0001-example/
      task.yaml
      journal.md
      subagents/
      reports/
      artifacts/
```

Use `.litehive/context.md` to describe the repo, commands, and workflow conventions that every future subagent run should inherit.

## Git checkpoints

By default, Litehive records a git checkpoint whenever a task reaches `done` and the workspace is a git repository.
The task stores the checkpoint policy in `task.yaml`, including the commit subject, the base `HEAD`, and the number
of completed attempts.

- Default checkpoint subject: `litehive: checkpoint <task-id> <slug>`
- Repeat completion attempts append an attempt suffix: `litehive: checkpoint <task-id> <slug> (attempt N)`
- `litehive rollback <task-id>` reverts the latest checkpoint commit, creates a rollback commit, and requeues the task at `implementing`
- `litehive recover <task-id>` requeues a completed task at `implementing` without reverting code

Rollback and recover are only valid for completed tasks. Rollback also requires a clean git worktree so the revert is deterministic.
