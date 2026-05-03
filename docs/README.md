# litehive Documentation

Litehive is a local-first task runner for software projects. It stores
workspace configuration in `.litehive/config.yaml`, stores runtime data under
`${LITEHIVE_HOME:-$XDG_DATA_HOME/litehive}`, and runs queued tasks through a
fixed agent pipeline.

The docs in this directory are current reference material. Historical
refactoring audits and implementation plans were removed from this folder
because they described completed migrations or temporary cleanup work. New
implementation work should be tracked as Litehive tasks, not as long-lived docs.

## Current Docs

- [domain.md](domain.md): canonical domain vocabulary, model ownership, storage
  rules, and naming rules.
- [state-machine.md](state-machine.md): task statuses, pipeline states, operator
  transitions, queue eligibility, and recovery semantics.
- [code-style.md](code-style.md): local code style decisions that are easy to
  regress during refactors.

## Useful Runtime References

- `litehive --help`: current command list.
- `litehive task evidence <task_id>`: compact task evidence for agents and
  operators.
- `litehive pipeline journal <task_id>`: state-machine trace for a task.
- `litehive health`: workspace health diagnostics.
- `litehive repair`: stale active task, interrupted run, and queue repair.

## Documentation Policy

Keep durable concepts here. Do not keep completed implementation plans,
one-time migration notes, or task queues in `docs/`; those become stale quickly
and should live in Litehive tasks or commit history instead.
