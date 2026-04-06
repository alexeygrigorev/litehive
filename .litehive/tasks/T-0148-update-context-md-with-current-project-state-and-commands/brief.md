# T-0148 Update context.md with current project state and commands

- Mode: tasks
- Task type: docs
- PM complexity: simple
- Planned effort: s

## Goal
context.md is stale. Update it to reflect current CLI commands, engine list, pipeline modes, hooks, daemon, recovery agents, and workspace layout.

## Acceptance Criteria
- .litehive/context.md is updated to match the current Litehive operator surface for commands and workflow concepts, including task/task-editing commands, run modes, daemon commands, recovery commands, and reporting commands present in the live CLI/docs.
- .litehive/context.md reflects the current engine inventory and routing model, including the supported adapters, recovery engine support, and the distinction between task mode and run mode where relevant to operator guidance.
- .litehive/context.md reflects the current workspace and execution model, including hooks, recovery agents, and the durable-vs-runtime workspace layout/artifact split documented in the repo.

## Constraints
- Keep changes scoped to the task.

## Plan
- Compare .litehive/context.md against the current command surface and docs sources in docs/cli.md, docs/pipeline.md, docs/configuration.md, docs/engines.md, docs/recovery.md, and docs/workspace-layout.md.
- Update .litehive/context.md with a concise current-state summary covering commands, engines, pipeline/task modes, hooks, daemon/recovery behavior, and workspace layout without drifting into duplicate full-manual prose.
- Verify the updated context against the live CLI help and targeted docs/source references, then report the specific sources used as evidence.

## PM Sizing
- Complexity: simple
- Planned effort: s
