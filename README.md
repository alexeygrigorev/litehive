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
litehive status
# edit .litehive/config.yaml before starting work if you need non-default settings

# Add some tasks
litehive task add "Add user authentication" --goal "Users can sign up and log in"
litehive task add "Fix the search bug" --goal "Search returns results for partial matches"
litehive task add "Write API documentation" --task-type docs

# Start the daemon
litehive start

# Check progress
litehive status
litehive status
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

You can update task metadata in place:

```bash
litehive task add "Refactor the database layer"
litehive task update T-0005 --title "Refactor the database access layer"
```

When an engine hits its quota limit, litehive can fall back to another engine automatically.

## CLI commands

Task management:

```bash
litehive task add "Task title" --goal "What needs to happen"
litehive task add "Research task" --task-type research --mode tasks
litehive task update T-0001 --title "Sharper task title" --priority high
litehive queue move T-0003 1                    # move to position 1
litehive queue promote T-0005                   # move to front
litehive queue requeue T-0002 --front           # requeue a flagged task
litehive task close T-0004 --outcome wont_do --reason "No longer needed"
litehive task abandon T-0006
```

Execution:

```bash
litehive run                              # run one task
litehive run --drain                      # run until queue is empty
litehive start                       # start background daemon
litehive stop                      # stop daemon
litehive status                    # quick runner and queue state
litehive task logs --daemon        # recent background-runner sessions
```

Monitoring:

```bash
litehive status                           # quick workspace overview
litehive status --fast                    # legacy alias for the default quick read
litehive status --full                    # verbose per-task status dump
litehive queue                            # show queue order
litehive task logs                             # tail the latest daemon run log
litehive task logs --daemon                    # list recent daemon sessions with outcomes
litehive task logs T-0002                      # print the task journal
litehive task logs T-0002 --agent              # show the latest subagent transcript/stdout tail
litehive task logs T-0002 --agent --all        # list all subagent runs for a task
litehive task logs --follow                    # follow the active subagent stdout live
litehive task debug T-0002 --worktree         # inspect recorded worktree existence and changes
litehive worktree ls                      # list Litehive-managed worktrees with task status and change counts
litehive worktree clean --dry-run         # preview cleanup of closed-task worktrees
```

Recovery:

```bash
litehive repair                           # fix stale state
litehive rollback T-0001                  # revert a completed task
litehive queue requeue T-0001                   # requeue without reverting
litehive queue resume T-0002                    # resume an interrupted task
```

Queue and recovery shortcuts:

```bash
litehive recover T-0001                   # requeue a completed task but keep its code in place
litehive switch T-0002 gemini --reason "Need larger context window"
litehive prioritize T-0007 T-0003 T-0009  # move queued tasks to the front in this exact order
```

Use `litehive recover <task_id>` when a completed task needs another pipeline pass but the accepted code should remain in the workspace and git history. Use `litehive rollback <task_id>` instead when you need to revert the code before retrying.

Use `litehive switch <task_id> <engine> --reason "..."` when the task should continue on a different engine next time it runs. Litehive records the handoff reason, stops or detaches the current run if needed, and requeues the task for the next iteration.

Use `litehive prioritize <task_id> [task_id ...]` when several tasks are already queued and you need to pull them to the front without changing their relative order. The command preserves the exact order you pass on the command line.

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
- If a live Codex subagent goes 5 minutes without new stdout, Litehive terminates that subprocess and lets the normal stage retry flow restart the same stage
- The recovery engine can be different from the task engine (e.g. use Claude for recovery while Codex does the work)

That 5-minute live-subagent timeout is separate from daemon stale-runner recovery. It applies to stalled subprocess output, not to background daemon lock or heartbeat repair.

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
subagent_inactivity_timeout_seconds: 300.0
inactivity_timeout_seconds: null

# Hooks that run before/after stages
runner_hooks:
  before_pm_acceptance:
    - command: "uv run ruff check ."
      blocking: true
      description: "ensures lint passes before acceptance"
  after_swe_implementation:
    - command: "uv run pytest -x -q"
      blocking: false
      description: "runs the focused post-implementation regression slice"

# Agent-specific startup guidance
agent_startup_guidance:
  all:
    - Start from the latest task artifacts before broad repo exploration.
  swe:
    - Prefer targeted file reads over full repo scans.
  qa:
    - Read the latest implementing report before running tests.
```

`subagent_inactivity_timeout_seconds` controls live subagent stdout stall detection. For Codex, Litehive kills the subprocess after 300 seconds without new output and relies on the existing retry path to restart the stage, reusing continuation or resume ids when the engine produced one. `inactivity_timeout_seconds` is the separate top-level runner timeout.

Describe your project in `.litehive/context.md` so agents understand the codebase:

```bash
litehive status
```

Litehive bootstraps `.litehive/config.yaml` and `.litehive/context.md` on first run. Edit them by hand after bootstrap. Available profiles: generic, python, django, rust.

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
      comments.yaml    # agent discussion history (gitignored)
      journal.md       # event log (gitignored)
  worktrees/           # git worktrees for task isolation (gitignored)
  logs/                # daemon and run-all logs (gitignored)
  runtime/             # live coordination state (gitignored)
```

Each task runs in its own git worktree. When it passes all stages, the worktree is merged into main and cleaned up.

## Artifact retention

Litehive keeps `task.yaml`, `runtime.yaml`, stage reports, `comments.yaml`, `journal.md`, `events.jsonl`, `session.yaml`, and `report.yaml` as the durable evidence surface for status, repair, recovery, and handoff.

High-volume raw execution artifacts are treated as disposable support data:

- Only the latest subagent attempt keeps raw `prompt`, transcript, stdout/stderr, and timeline artifacts; older subagent folders keep their `session.yaml` and `report.yaml` but have raw files pruned.
- Final subagent transcript, stdout/stderr, timeline, and large runner hook artifacts may be stored as gzip snapshots when they are large; readers are expected to handle both plain and `.gz` files.
- Daemon `logs/run-all/` sessions are bounded to the most recent 8 directories so repeated pool runs do not accumulate unbounded wrapper logs.

## Daemon

The daemon runs tasks continuously in the background:

```bash
litehive start
```

Each iteration spawns a fresh subprocess, so code changes to litehive itself are picked up automatically without restarting.

```bash
litehive status                    # this workspace
litehive task logs --daemon   # recent runner sessions
litehive stop
litehive restart
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
litehive queue requeue T-0001      # requeues without reverting (keeps the code)
```

## Running on multiple projects

Install litehive once, use it anywhere:

```bash
# Project A
cd ~/projects/webapp
litehive status
litehive start

# Project B
cd ~/projects/api-server
litehive status
litehive start

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

## Best Practices for Tests

### Speed requirements

- **Each unit test has a 60-second hard timeout** enforced by `pytest-timeout` in `pyproject.toml` — tests exceeding this are killed automatically
- **Each test file should finish within 1 minute** — if a test file takes longer, the tests need to be optimized or split
- **The full test suite must finish within 3 minutes** (`uv run pytest tests/ -q`) — if a change pushes it over 3 minutes, QA must reject
- **Integration tests** (in `tests_integration/`) may take up to 3 minutes per file
- A live subagent is killed after **5 minutes of no output** and the normal retry flow restarts the stage — run only the specific test file for your change, not the full suite
- `LITEHIVE_SKIP_FSYNC=1` is set automatically in tests via `conftest.py` to avoid slow disk flushes

### Always mock the execution layer

Any test that calls `run_next_task`, `run_task`, `drain_task_pool`, `_cmd_run`, or `TaskExecutionRunner.run` MUST mock the execution layer. Without mocks, tests will try to invoke real engine CLIs (claude, codex, etc.) and hang forever.

Two levels of mocking, pick one:

**Option A: Mock SubagentManager.run** (most common, tests the full runner pipeline):
```python
def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):
    return SubagentResult(
        ref=SubagentRef(id="SA-stub", role=role, engine=engine_name, status="completed", path="subagents/stub"),
        execution=CLIExecutionResult(adapter=engine_name, argv=(engine_name, "exec"), cwd=tmp_path, exit_code=0,
            stdout="VERDICT: PASS\nSUMMARY: ok\nFILES_CHANGED:\n- app.txt\nTESTS_ADDED: 1\nTESTS_PASSING: 1\nWARNINGS:\n",
            stderr=""),
        transcript="", exit_code=0,
    )
monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)
```

**Option B: Custom executor** (bypasses SubagentManager, tests runner logic only):
```python
def executor(task, step):
    return StageReport(
        task_id=task.id, step=step, verdict="pass", summary=f"{step} ok",
        files_changed=["app.txt"], tests={"added": 1, "passing": 1},
    )
runner = TaskExecutionRunner(tmp_path, executor)
```

### Fake implementing passes must include files

The runner has an empty SWE guard: if implementing reports "pass" with zero files changed and zero tests added, it rejects and retries. If your fake always returns empty results, this creates an infinite loop.

**Wrong** — causes infinite retry loop:
```python
StageReport(task_id=task.id, step=step, verdict="pass", summary="ok")
# files_changed defaults to [], tests defaults to {"added": 0, "passing": 0}
```

```python
stdout = "VERDICT: PASS\nSUMMARY: ok\nFILES_CHANGED:\nTESTS_ADDED: 0\nTESTS_PASSING: 0\nWARNINGS:\n"
```

**Right** — includes at least one file:
```python
StageReport(task_id=task.id, step=step, verdict="pass", summary="ok",
    files_changed=["app.txt"], tests={"added": 1, "passing": 1})
```

```python
stdout = "VERDICT: PASS\nSUMMARY: ok\nFILES_CHANGED:\n- app.txt\nTESTS_ADDED: 1\nTESTS_PASSING: 1\nWARNINGS:\n"
```

This only matters for implementing stage passes. FAIL/REJECT verdicts and non-implementing stages (grooming, testing, accepting) don't need files.

### Match current function signatures in fakes

When creating fake engine adapters or `run_live` functions, match the real function signature including all keyword arguments. If a new parameter is added to the real function, all fakes must accept it too (use `**kwargs` if unsure).

Common parameters that get added over time:
- `on_started` callback in `ClaudeCLIAdapter.run`
- `inactivity_timeout_seconds` in `run_live`
- `resume_session_id` in adapter run methods

### Monkeypatch the right module path

When a function is imported with `from module import func`, monkeypatch the import site, not the source module:
```python
# If runner/core.py does: from litehive.tasks import save_task
# Patch at the import site:
monkeypatch.setattr("litehive.pipeline.core.save_task", fake)
# NOT at the source:
monkeypatch.setattr("litehive.tasks.save_task", fake)  # won't work
```

### Use workspace helpers

The `tests/workspace_helpers.py` module provides tested helper functions. Use them instead of writing inline fakes:
- `_completed_subagent_result(tmp_path, step)` — SubagentResult with files for any stage
- `_stage_subagent_result(cwd, step, verdict=..., files_changed=...)` — customizable SubagentResult
- `_init_git_repo(tmp_path)` — creates a minimal git repo for commit tests
- `_commit_repo_state(cwd, message)` — commits current state

### Avoid real git operations when not needed

Each `_init_git_repo()` call spawns 5+ git subprocesses (~100-150ms). Tests that don't need real git history should use `auto_commit=False` in `create_task()` to skip worktree creation.

### No `time.sleep()` in tests

Replace `time.sleep()` with polling loops or mock the clock. If you must sleep, keep it under 0.5s and document why.
