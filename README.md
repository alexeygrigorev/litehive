# litehive

Local-first autonomous task execution for software projects.

Litehive keeps a task queue for a repository and runs each task through coding
agents such as Codex, Claude, Copilot, Gemini, OpenCode, and Goz. The runner
grooms the task, implements it, verifies it, reviews it, commits the result, and
records the pipeline history in SQLite.

```text
grooming -> implementing -> testing -> accepting -> commit_to_git
```

## Quick Start

```bash
git clone git@github.com:alexeygrigorev/litehive.git
cd litehive
uv sync

cd /path/to/your/project
litehive status
```

The first `litehive status` in a project bootstraps `.litehive/config.yaml` and
`.litehive/context.md`. Edit those files before starting work if the defaults
are not right for the project.

Add tasks:

```bash
litehive task add "Add user authentication" --goal "Users can sign up and log in"
litehive task add "Fix search results" --goal "Partial search terms return matching records"
litehive task add "Document API auth" --goal "Capture the public auth flow for integrators"
```

Run one task or start the background runner:

```bash
litehive run
litehive start
litehive status
```

Litehive resolves `heru>=2.0.1` from a sibling `../heru` checkout during
`uv sync`. Clone or update that checkout next to this repository before syncing;
`pyproject.toml` pins it through `[tool.uv.sources]` as an editable path
dependency.

## Core Commands

Task management:

```bash
litehive task add "Task title" --goal "What needs to happen"
litehive task update T-0001 --title "Sharper task title" --priority high
litehive task list
litehive task show T-0001
litehive task evidence T-0001
litehive task close T-0001 --outcome wont_do --reason "No longer needed"
```

Queue and execution:

```bash
litehive queue
litehive queue move T-0003 1
litehive queue promote T-0005
litehive queue requeue T-0002
litehive queue resume T-0004
litehive run
litehive run --drain
litehive start
litehive stop
litehive restart
```

Monitoring and recovery:

```bash
litehive status
litehive health
litehive task logs
litehive task logs --daemon
litehive task logs T-0001
litehive task logs T-0001 --agent
litehive pipeline journal T-0001
litehive repair
```

Engine and worktree inspection:

```bash
litehive engine status
litehive queue switch T-0001 gemini --reason "Need a larger context window"
litehive worktree ls
litehive worktree clean --dry-run
```

Run `litehive --help` and subcommand help for the current command surface.

## Configuration

Workspace config lives in `.litehive/config.yaml`. Global defaults live in
`${LITEHIVE_HOME:-$XDG_DATA_HOME/litehive}/config.yaml`, usually
`~/.local/share/litehive/config.yaml`. Workspace settings take precedence.

Minimal workspace config:

```yaml
default_engine: codex
recovery_engine: claude
claude_model: claude-opus-4-6
auto_commit: true
subagent_inactivity_timeout_seconds: 300.0
```

Common hook configuration:

```yaml
runner_hooks:
  after_implementing:
    - command: "uv run pytest -x -q"
      description: "runs the focused post-implementation regression slice"
  before_accepting:
    - command: "uv run ruff check ."
      description: "ensures lint passes before acceptance"
```

Use `.litehive/context.md` to describe the project, expected verification
commands, and agent guidance. Available process profiles are `generic`,
`python`, `django`, and `rust`.

## Engines

Litehive calls external agent CLIs as subprocesses. Supported engine names are:

- `codex`
- `claude`
- `copilot`
- `gemini`
- `opencode`
- `goz`

Engine selection comes from run-time overrides, the workspace `default_engine`,
global engine preferences, and quota or availability checks. Task metadata does
not store an engine field. Use `litehive queue switch <task_id> <engine>
--reason "..."` to record an explicit handoff for the next run.

## Workspace Layout

Repo-local files:

```text
.litehive/
  config.yaml
  context.md
  .gitignore
```

Runtime state lives outside the repository:

```text
~/.local/share/litehive/
  config.yaml
  workspaces.db
  <workspace-id>/
    data.db
    backups/
    logs/
    runtime/
    subagents/
    worktrees/
```

Task intent, queue state, runtime status, activity, reports, subagent sessions,
pipeline history, monitoring, and audit data are stored in SQLite. The only
Litehive-owned YAML file that should remain in a workspace is
`.litehive/config.yaml`.

Set `LITEHIVE_HOME=/custom/path` to use a different runtime root.

## Git Integration

Each task runs in its own git worktree. When the task passes all stages,
Litehive commits the task worktree, merges it into the main workspace, and
pushes the result. If a merge conflict occurs, Litehive routes through merge
resolution before the task is finalized.

Commit format:

```text
litehive: complete T-0001 task-slug
```

Use `litehive queue requeue <task_id>` when a completed task needs another
pipeline pass without reverting the accepted code.

## Querying Data

Resolve the current workspace database:

```bash
python -c "from pathlib import Path; from litehive.config.paths import workspace_path; print(workspace_path(Path.cwd(), 'data.db'))"
```

Useful tables include `task_state`, `queue`, `pipeline_journal`,
`pipeline_transitions`, `stage_reports`, `subagent_sessions`,
`engine_monitoring`, `attention`, and `worktrees`.

Prefer direct SQL for broad reports. Use CLI commands for focused inspection:

```bash
litehive status
litehive task list
litehive task show T-0001
litehive task evidence T-0001
litehive pipeline journal T-0001
```

## Documentation

Current reference docs live in [docs/](docs/):

- [docs/domain.md](docs/domain.md): canonical domain vocabulary and storage
  rules.
- [docs/state-machine.md](docs/state-machine.md): task and pipeline lifecycle
  reference.
- [docs/code-style.md](docs/code-style.md): local style decisions.

Historical refactoring plans are intentionally not kept in `docs/`; completed
implementation notes should live in task history and commits.

## Development

Run the unit suite:

```bash
uv run pytest -q
```

Run linting:

```bash
uv run ruff check .
```

Integration tests live in `tests_integration/` and are opt-in because they
require locally installed, authenticated engine CLIs:

```bash
LITEHIVE_INTEGRATION_ENGINES=codex uv run pytest tests_integration/ -q
```
