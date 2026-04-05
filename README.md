# litehive

Autonomous task execution for software projects using AI coding agents.

litehive manages a queue of tasks and runs them through a structured pipeline using AI coding agents (Codex, Claude, Copilot, Gemini, OpenCode, and others). Each task goes through grooming, implementation, testing, acceptance, and git commit - all without human intervention. When something breaks, a recovery agent investigates and fixes it automatically.

You describe what needs to be done. litehive figures out how to get it done, assigns it to an agent, verifies the result, and commits the code.

## How it works

1. You add tasks to a queue
2. litehive picks the next task and sends it through a pipeline
3. Different agents handle different stages - a planner grooms the task, an engineer implements it, a QA agent tests it, a reviewer accepts it
4. When everything passes, the code is committed to git and pushed
5. If something fails, a recovery agent diagnoses the problem and fixes it
6. The queue keeps moving until all tasks are done

The pipeline for each task:

```
grooming -> implementing -> testing -> accepting -> commit_to_git
```

## Quick start

```bash
# Install
git clone git@github.com:alexeygrigorev/litehive.git
cd litehive
uv sync

# Set up a project
cd /path/to/your/project
litehive configure

# Add some tasks
litehive add "Add user authentication" --goal "Users can sign up and log in"
litehive add "Fix the search bug" --goal "Search returns results for partial matches"
litehive add "Write API documentation" --task-type docs

# Start the daemon
litehive daemon run

# Check progress
litehive status
litehive daemon status
```

## Engines

litehive works with multiple AI coding agents. Each one is a separate CLI tool that litehive calls as a subprocess:

- codex - OpenAI Codex CLI (gpt-5.4-high, o3, etc.)
- claude - Anthropic Claude Code CLI
- copilot - GitHub Copilot CLI
- gemini - Google Gemini CLI
- opencode - OpenCode CLI (Z.AI models)
- goz - Goz CLI (Z.AI models)

Set your default engine in `.litehive/config.yaml`:

```yaml
default_engine: codex
codex_model: gpt-5.4-high
```

You can also set engines per task:

```bash
litehive add "Refactor the database layer" --engine claude
litehive update T-0005 --engine copilot
```

When an engine hits its quota limit, litehive can fall back to another engine automatically.

## CLI commands

Task management:

```bash
litehive add "Task title" --goal "What needs to happen"
litehive add "Research task" --task-type research --mode tasks
litehive update T-0001 --engine opencode --priority high
litehive move T-0003 1                    # move to position 1
litehive promote T-0005                   # move to front
litehive requeue T-0002 --front           # requeue a flagged task
litehive close T-0004 --outcome wont_do --reason "No longer needed"
litehive abandon T-0006
```

Execution:

```bash
litehive run                              # run one task
litehive run --drain                      # run until queue is empty
litehive run --dry-run                    # preview what would run
litehive daemon run                       # start background daemon
litehive daemon stop                      # stop daemon
litehive daemon status                    # check daemon state
litehive daemon instances                 # list all running daemons
```

Monitoring:

```bash
litehive status                           # workspace overview
litehive status --fast                    # quick state-only read
litehive queue                            # show queue order
litehive web                              # local web dashboard
```

Recovery:

```bash
litehive repair                           # fix stale state
litehive rollback T-0001                  # revert a completed task
litehive recover T-0001                   # requeue without reverting
litehive resume T-0002                    # resume an interrupted task
```

Agent interaction:

```bash
litehive report --verdict pass --role qa --step testing --message "All tests pass"
litehive report --verdict reject --role qa --step testing --message "Expected: login returns 200. Observed: returns 500."
```

## Self-healing

When a stage fails or an agent crashes, litehive does not just give up:

- If an agent crashes or returns an error, a recovery agent is launched to investigate and fix the problem
- If a merge conflict occurs during commit, a merge resolution agent resolves it
- If the same stage fails 3 or more times, the task gets escalated back to grooming for replanning
- If an engine hits its quota, litehive switches to another engine
- The recovery engine can be different from the task engine (e.g. use Claude for recovery while Codex does the work)

Configure the recovery engine:

```yaml
recovery_engine: claude
```

## Configuration

Workspace config lives in `.litehive/config.yaml`. Global defaults go in `~/.config/litehive/config.yaml`. Workspace settings take precedence.

```yaml
default_engine: codex
recovery_engine: claude
codex_model: gpt-5.4-high
claude_model: claude-opus-4-6
auto_commit: true

# Hooks that run before/after stages
runner_hooks:
  before_pm_acceptance:
    - command: "uv run ruff check ."
      blocking: true
  after_swe_implementation:
    - command: "uv run pytest -x -q"
      blocking: false

# Agent-specific startup guidance
agent_startup_guidance:
  all:
    - Start from the latest task artifacts before broad repo exploration.
  swe:
    - Prefer targeted file reads over full repo scans.
  qa:
    - Read the latest implementing report before running tests.
```

Describe your project in `.litehive/context.md` so agents understand the codebase:

```bash
litehive configure --process-profile python
```

This generates a context template you can customize. Available profiles: generic, python, django, rust.

## Workspace layout

```
.litehive/
  config.yaml          # workspace configuration
  context.md           # project description for agents
  state.yaml           # queue and active task state
  .gitignore           # keeps runtime artifacts out of git
  tasks/
    T-0001-example/
      task.yaml        # task definition and status
      brief.md         # structured task brief
      reports/         # stage verdicts (gitignored)
      subagents/       # execution artifacts (gitignored)
      thread.yaml      # agent discussion history (gitignored)
      journal.md       # event log (gitignored)
  worktrees/           # git worktrees for task isolation (gitignored)
  logs/                # daemon and run-all logs (gitignored)
  runtime/             # live coordination state (gitignored)
```

Each task runs in its own git worktree. When it passes all stages, the worktree is merged into main and cleaned up.

## Artifact retention

Litehive keeps `task.yaml`, `runtime.yaml`, stage reports, `thread.yaml`, `journal.md`, `events.jsonl`, `session.yaml`, and `report.yaml` as the durable evidence surface for status, repair, recovery, and handoff.

High-volume raw execution artifacts are treated as disposable support data:

- Only the latest subagent attempt keeps raw `prompt`, transcript, stdout/stderr, and timeline artifacts; older subagent folders keep their `session.yaml` and `report.yaml` but have raw files pruned.
- Final subagent transcript, stdout/stderr, timeline, and large runner hook artifacts may be stored as gzip snapshots when they are large; readers are expected to handle both plain and `.gz` files.
- Daemon `logs/run-all/` sessions are bounded to the most recent 8 directories so repeated pool runs do not accumulate unbounded wrapper logs.

## Daemon

The daemon runs tasks continuously in the background:

```bash
litehive daemon run
```

Each iteration spawns a fresh subprocess, so code changes to litehive itself are picked up automatically without restarting.

```bash
litehive daemon status                    # this workspace
litehive daemon instances                 # all workspaces
litehive daemon stop
litehive daemon restart
```

## Git integration

When a task passes all stages, litehive commits the changes to git:

1. All changes in the task worktree are committed
2. The worktree is merged into main
3. If there are merge conflicts, a resolution agent fixes them
4. The result is pushed to the remote

Commit format: `litehive: complete T-0001 task-slug`

To undo a completed task:

```bash
litehive rollback T-0001     # reverts the commit and requeues the task
litehive recover T-0001      # requeues without reverting (keeps the code)
```

## Running on multiple projects

Install litehive once, use it anywhere:

```bash
# Project A
cd ~/projects/webapp
litehive configure
litehive daemon run

# Project B
cd ~/projects/api-server
litehive configure
litehive daemon run

# See all running daemons
litehive daemon instances
```

All instances share quota tracking at `~/.config/litehive/quota.yaml` so they coordinate engine usage across projects.

All commands default to the current directory. If you need to target a different project without changing directories, use `--workspace /path/to/project`.

## Engines

Currently supported:

- codex - OpenAI Codex CLI
- claude - Anthropic Claude Code
- copilot - GitHub Copilot CLI
- gemini - Google Gemini CLI
- opencode - OpenCode CLI
- goz - Goz CLI
