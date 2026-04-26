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
litehive task add "Write API documentation" --goal "Capture the public API surface and auth flow"

# Start the background runner
litehive start

# Check progress
litehive status
litehive status
```

Litehive resolves `heru` from the vendored wheel in `packages/` during `uv sync`. If you update the standalone `heru` package, rebuild that wheel before syncing this repo again.

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

Task metadata does not store an `engine` field. Engine selection comes from the
workspace `default_engine`, explicit run-time overrides, or a recorded
`litehive engine switch` handoff between runs.

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
litehive task add "Research task" --goal "Investigate the failure mode and summarize findings"
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
litehive start                            # start the background runner
litehive stop                             # stop the background runner
litehive status                           # quick runner and queue state
litehive task logs --daemon               # recent background-runner sessions
```

Monitoring:

```bash
litehive status                           # quick workspace overview
litehive status --fast                    # legacy alias for the default quick read
litehive status --full                    # verbose per-task status dump
litehive queue                            # show queue order
litehive task logs                        # tail the latest background-run log
litehive task logs --daemon               # list recent background-run sessions with outcomes
litehive task logs T-0002                      # print the task journal
litehive task logs T-0002 --agent              # show the latest subagent execution trace/stdout tail
litehive task logs T-0002 --agent --all        # list all subagent runs for a task
litehive task logs --follow                    # follow the active subagent stdout live
litehive task debug T-0002 --worktree         # inspect recorded worktree existence and changes
litehive worktree ls                      # list Litehive-managed worktrees with task status and change counts
litehive worktree clean --dry-run         # preview cleanup of closed-task worktrees
```

Recovery:

```bash
litehive repair                           # fix stale state
litehive queue requeue T-0001            # requeue a completed task without reverting accepted code
litehive queue resume T-0002             # resume an interrupted task
litehive engine switch T-0003 gemini --reason "Need larger context window"
```

Queue control:

```bash
litehive queue move T-0003 1             # move to an exact queue position
litehive queue promote T-0007            # move to the front
litehive queue requeue T-0001            # restart from the implementation entry stage
litehive queue resume T-0002             # resume at the current stage
```

Use `litehive queue requeue <task_id>` when a completed task needs another pipeline pass but the accepted code should remain in the workspace and git history.

Use `litehive engine switch <task_id> <engine> --reason "..."` when the task should continue on a different engine next time it runs. Litehive records the handoff reason, stops or detaches the current run if needed, and requeues the task for the next iteration.

Use `litehive queue move` or `litehive queue promote` when operator ordering matters more than the current queue order.

Agent interaction:

```bash
litehive report --verdict pass --stage testing --message "All tests pass"
litehive report --verdict reject --stage testing --message "Expected: login returns 200. Observed: returns 500."
```

## Self-healing

When a stage fails or an agent crashes, litehive does not just give up:

- If an agent crashes or returns an error, a recovery agent is launched to investigate and fix the problem
- If a merge conflict occurs during commit, a merge resolution agent resolves it
- If the same stage fails 3 or more times, the task gets escalated back to grooming for replanning
- If an engine hits its quota, litehive switches to another engine
- If a live Codex subagent goes 5 minutes without new stdout, Litehive terminates that subprocess and lets the normal stage retry flow restart the same stage
- The recovery engine can differ from the workspace default or current execution engine (e.g. use Claude for recovery while Codex handles normal runs)

That 5-minute live-subagent timeout is separate from background-runner stale-state recovery. It applies to stalled subprocess output, not to the runner lock or heartbeat repair.

Configure the recovery engine:

```yaml
recovery_engine: claude
```

## Configuration

Workspace config lives in `.litehive/config.yaml`. Global defaults live in `${LITEHIVE_HOME:-$XDG_DATA_HOME/litehive}/config.yaml` (default `~/.local/share/litehive/config.yaml`). Workspace settings take precedence.

On first run after upgrade, Litehive migrates legacy `~/.config/litehive/config.yaml` and `daemons.yaml` into the unified root, imports `~/.config/litehive/workspaces.yaml` into the unified registry database, and prints a deprecation notice.

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
  planner:
    - Rewrite scope with `litehive task update ...` before passing grooming.
  swe:
    - Prefer targeted file reads over full repo scans and finish with `litehive report ...`.
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

Repo-local control files stay in the repository:

```
.litehive/
  config.yaml          # workspace configuration
  context.md           # project description for agents
  .gitignore           # keeps runtime artifacts out of git
  tasks/
    T-0001-example/
      reports/         # stage verdict artifacts
      subagents/       # execution artifacts
      artifacts/        # supporting files captured for repair/debugging
```

Global Litehive state lives under one XDG root:

```
~/.local/share/litehive/
  config.yaml          # global defaults
  workspaces.db        # registered workspaces
  daemons.yaml         # running daemon index
  <workspace-id>/
    data.db            # workspace runtime database
    backups/           # compressed database backups
    logs/
      run-all/         # daemon/run iterations
    runtime/           # lock files and live coordination state
    subagents/         # shared subagent session data
    worktrees/         # git worktrees for task isolation
```

Set `LITEHIVE_HOME=/custom/path` to override that root for tests or alternate installs.

Task intent, queue state, runtime status, activity, and pipeline history are stored in the per-workspace `data.db` under this root. Litehive no longer uses `.litehive/state.yaml` or `~/.config/litehive` as active state locations; old files in `~/.config/litehive` are imported into the unified root on first run.

Each task runs in its own git worktree. When it passes all stages, the worktree is merged into main and cleaned up.

## Querying workspace data

Pipeline state lives in a single SQLite database under the unified Litehive root so it doesn't pollute git history. The workspace directory name is derived from a hash of the workspace path:

```
~/.local/share/litehive/<workspace-hash>/data.db
```

For the current workspace, resolve it with:

```bash
python -c "from pathlib import Path; from litehive.config.paths import workspace_path; print(workspace_path(Path.cwd(), 'data.db'))"
```

Useful tables: `pipeline_transitions`, `task_state`, `pipeline_journal`, `queue`, `stage_reports`, `subagent_sessions`, `engine_monitoring`, `attention`, `worktrees`.

Example — transitions + duration + status for every task touched in the last 24h (single query, instant):

```bash
DB=$(python -c "from pathlib import Path; from litehive.config.paths import workspace_path; print(workspace_path(Path.cwd(), 'data.db'))")
sqlite3 "$DB" <<'SQL'
.mode column
.headers on
SELECT
  t.task_id,
  COUNT(*) AS transitions,
  CAST((julianday(MAX(t.created_at)) - julianday(MIN(t.created_at))) * 24 * 60 AS INTEGER) AS dur_min,
  json_extract(s.payload, '$.status') AS status
FROM pipeline_transitions t
LEFT JOIN task_state s ON s.task_id = t.task_id
WHERE t.created_at > datetime('now', '-24 hours')
GROUP BY t.task_id
ORDER BY t.task_id;
SQL
```

Prefer direct SQL over iterating `litehive task show` per task — the CLI per-task approach is ~100× slower on any non-trivial report.

CLI equivalent for the same recent-task summary:

```bash
litehive task recent             # tasks touched in the last 24h
litehive task recent --since 72h # widen the reporting window
```

## Artifact retention

Litehive keeps task intent, queue state, runtime status, activity, and pipeline history in SQLite. Stage reports, `events.jsonl`, `session.yaml`, `report.yaml`, and raw execution artifacts remain as the file-backed evidence surface for repair, recovery, and handoff.

During migration, Litehive imports legacy per-task activity YAML files into SQLite and removes them after a successful import.

High-volume raw execution artifacts are treated as disposable support data:

- Only the latest subagent attempt keeps raw `prompt`, execution trace, stdout/stderr, and event stream artifacts; older subagent folders keep their `session.yaml` and `report.yaml` but have raw files pruned.
- Final subagent execution trace, stdout/stderr, event stream, and large runner hook artifacts may be stored as gzip snapshots when they are large; readers are expected to handle both plain and `.gz` files.
- Background-run `logs/run-all/` sessions are bounded to the most recent 8 directories so repeated pool runs do not accumulate unbounded wrapper logs.

## Background Runner

The background runner drains tasks continuously after `litehive start`:

```bash
litehive start
```

Each iteration spawns a fresh subprocess, so code changes to litehive itself are picked up automatically without restarting.

```bash
litehive status                    # this workspace
litehive task logs --daemon        # recent runner sessions
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
litehive queue requeue T-0001      # requeues the task without reverting the accepted code
litehive queue resume T-0002       # resumes the task at its current stage
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

# Inspect recent background-runner sessions
litehive task logs --daemon
```

All global Litehive metadata for every project now lives under `${LITEHIVE_HOME:-$XDG_DATA_HOME/litehive}` so backup, cleanup, and debugging only require inspecting one root directory.

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
