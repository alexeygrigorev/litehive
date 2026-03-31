# litehive

Local-first autonomous coding workspace with deterministic task execution.

## v1 goals

- Single active task at a time
- Local YAML-backed workspace state
- Two modes: `tasks` and `implementation`
- Deterministic stage pipeline
- Subagents executed through external CLIs (`codex` and `opencode` first)
- Commit after successful task completion

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
