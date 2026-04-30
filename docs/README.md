# litehive Documentation

litehive is a local-first task runner for software projects. It stores task and
repo-local metadata under `.litehive/`, keeps global/runtime state under
`${LITEHIVE_HOME:-$XDG_DATA_HOME/litehive}`, routes work through a fixed
pipeline, and uses external coding-agent CLIs such as Codex, Gemini, OpenCode,
Copilot, Claude, and Goz to execute each stage.

This guide set is written for a first-time user. Start here, then use the
linked pages as reference once your workspace is running.

## What litehive does

- Keeps a queue of tasks in your repository.
- Runs each task through `grooming -> implementing -> testing -> accepting -> commit_to_git`.
- Persists task intent, queue state, runtime state, reports, monitoring, and
  audit data in SQLite. Repo-local `.litehive/` holds the workspace config and
  unstructured artifacts.
- Can recover interrupted or failed work and continue without losing context.
- Can run one task at a time or drain the whole queue through the background runner.

## Documentation Map

- [configuration.md](configuration.md): workspace config, global config, models,
  hooks, retry policies, sandboxing, and routing.
- [cli.md](cli.md): every CLI command with examples.
- [pipeline.md](pipeline.md): single-task runs, drain mode, stages, roles, and
  the state machine.
- [engines.md](engines.md): supported adapters, model resolution, fallbacks, and
  adding a new engine.
- [sandboxing.md](sandboxing.md): per-role sandbox profiles, git access policy,
  wrapper denylist, and manual breakout auditing.
- [recovery.md](recovery.md): repair, recovery agents, rollback, recover, and
  merge-conflict handling.
- [workspace-layout.md](workspace-layout.md): what lives under `.litehive/` and
  what is tracked versus ignored.
- [domain.md](domain.md): the target Litehive domain model and canonical terms
  for states, reports, recovery, and runtime artifacts.
- [domain-spec.md](domain-spec.md): the general template and review
  rules for writing domain documents.
- [state-machine.md](state-machine.md): the exhaustive transition reference used
  by the codebase.
- [contributing-back.md](contributing-back.md): filing upstream Litehive work
  from another project.

## Installation

Clone the repository and install dependencies:

```bash
git clone git@github.com:alexeygrigorev/litehive.git
cd litehive
uv sync
```

Use `uv run litehive ...` to run commands, or install the package with
`uv tool install --editable .` for a global `litehive` binary.

## Quick Start

Initialize a repository as a Litehive workspace:

```bash
cd /path/to/your/project
litehive status
# Litehive bootstraps .litehive/config.yaml on first run.
# Edit that file by hand before starting work if you need non-default settings.
```

Create a few tasks:

```bash
litehive task add "Fix queue ordering bug" \
  --goal "Dependency-blocked tasks do not jump ahead of runnable work." \
  --acceptance-criteria "Blocked tasks remain visible but are not selected before prerequisites finish." \
 

litehive task add "Document API auth flow" \
  --goal "Document the current auth flow for external integrators" \
 
```

Litehive does not import GitHub issues or specs directly. Normalize external
inputs with your own preprocessing, then create tasks through `litehive task add`.

Inspect the workspace:

```bash
litehive status
litehive queue
```

Run one task:

```bash
litehive run
```

Or run continuously in the background:

```bash
litehive start
litehive status
```

## First Concepts To Know

- The workspace root is your project root. Litehive keeps its own state in
  `.litehive/`.
- Task intent, queue state, runtime status, and pipeline history live in SQLite.
  The only LiteHive-owned workspace YAML file should be `.litehive/config.yaml`.
  Repo-local `.litehive/` task directories hold supporting evidence and
  artifacts.
- `litehive run` executes one selection cycle. `litehive run --drain` keeps
  going until Litehive reaches an explicit stop condition.
- `litehive repair` is the manual recovery entrypoint for stale active tasks,
  interrupted runs, and queue cleanup.
- Engine selection starts from any run override, then the workspace default,
  then global fallback preference and quota/availability checks.

## Minimal Daily Workflow

```bash
litehive status   # bootstraps .litehive/config.yaml on first run
litehive task add "Implement feature X" --goal "..." --acceptance-criteria "..."
litehive run
litehive status
litehive repair   # when a prior run was interrupted
```

Once that feels natural, move to [cli.md](cli.md) and [pipeline.md](pipeline.md).
