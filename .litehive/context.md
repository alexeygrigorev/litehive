# Litehive Workspace Context

## Project
- Purpose: build a local-first deterministic coding workspace with YAML-backed task state, fixed stage routing, and external coding-agent execution.
- Main package/module locations: `litehive/cli.py`, `litehive/config.py`, `litehive/tasks.py`, `litehive/runner.py`, `litehive/runtime.py`, `litehive/subagents.py`, `litehive/engines.py`, `litehive/daemon.py`, `litehive/observability.py`, `litehive/web.py`, `litehive/tui/app.py`.
- Main tests: `tests/test_workspace_bootstrap.py`, `tests/test_config_and_run_cli.py`, `tests/test_runner_workflow.py`, `tests/test_runtime_pool.py`, `tests/test_task_commands_and_daemon.py`, `tests/test_tasks_and_subagents.py`, `tests/test_observability_and_status.py`, `tests/test_recovery_agent.py`, `tests/test_retry_commit_and_recovery.py`, `tests/test_engine_variants_and_timeline.py`.

## Commands To Know
- `uv run pytest -q`
- `uv run litehive configure --workspace .`
  Initializes `.litehive/` and can seed routing, retry, hook, sandbox, and model defaults.
- `uv run litehive status --workspace .`
  Uses the default fast workspace summary path.
- `uv run litehive status --workspace . --fast`
  Legacy compatibility alias for the default fast summary path.
- `uv run litehive status --workspace . --full`
  Prints the fuller per-task workspace dump.
- `uv run litehive queue --workspace .`
- `uv run litehive tasks --workspace .`
  Opens the task TUI view.
- `uv run litehive web --workspace .`
  Starts the local HTTP monitor for queue state, task/session details, tailed artifacts, and recent daemon logs.
- `uv run litehive engine gemini --workspace .`
  Persists the workspace default engine in `.litehive/config.yaml`.
- `uv run litehive add "<title>" --workspace .`
- `uv run litehive add "<title>" --task-type docs --workspace .`
- `uv run litehive add "<title>" --task-type review --mode tasks --workspace .`
  `--task-type` defaults creation mode to `tasks`, which writes the task-folder shaping files such as `brief.md`; omit it or pass `--mode implementation` for the simpler implementation-style intake path.
- `uv run litehive issue --upstream "<title>" --workspace .`
  Files upstream Litehive follow-up work using `litehive_source_path` metadata and optional patch-branch handoff fields.
- `uv run litehive intake spec.md --workspace .`
  Creates a rough task from freeform text using an engine.
- `uv run litehive update T-0002 --engine opencode --priority high --workspace .`
- `uv run litehive update T-0002 --goal "..." --acceptance-criteria "..." --constraint "..." --plan-step "..." --workspace .`
- `uv run litehive update T-0002 --from-file task-shape.yaml --workspace .`
- `uv run litehive update T-0002 --edit --workspace .`
- `uv run litehive switch T-0002 gemini --reason "quota exhausted" --workspace .`
  Stops the active run if needed, records the engine switch in task artifacts, and requeues the task for the next pass with continuation context.
- `uv run litehive close T-0002 --outcome deferred --reason "waiting on upstream" --workspace .`
  Records an explicit non-implementation outcome such as `wont_do`, `deferred`, or `duplicate`.
- `uv run litehive move T-0002 1 --workspace .`
- `uv run litehive prioritize T-0004 T-0002 T-0003 --workspace .`
- `uv run litehive promote T-0002 --workspace .`
- `uv run litehive requeue T-0002 --front --workspace .`
- `uv run litehive resume T-0002 --workspace .`
- `uv run litehive abandon T-0002 --workspace .`
- `uv run litehive stop --workspace .`
  Stops the current active task cleanly.
- `uv run litehive run --workspace .`
  Runs one selection cycle and leaves remaining work queued.
- `uv run litehive run --workspace . --dry-run`
  Shows the next planned task, engine choice, and predicted stop reason without invoking agents.
- `uv run litehive run --workspace . --drain`
  Drains the runnable pool until Litehive reaches an explicit stop condition.
- `uv run litehive run --workspace . --drain --dry-run`
  Previews the pool order and stop reason without invoking agents.
- `uv run litehive run --workspace . --engine gemini --model gemini-2.5-pro`
  Run-time engine and model overrides win over task routing for that invocation.
- `uv run litehive repair --workspace .`
  Manual recovery entrypoint for stale active tasks, interrupted runs, stranded `commit_to_git`, and queue cleanup.
- `uv run litehive debug T-0002 --workspace . --worktree`
  Shows whether the recorded task worktree exists, plus uncommitted files and committed changes ahead of main from that worktree.
- `uv run litehive worktree ls --workspace .`
  Lists Litehive-managed task worktrees with task status and per-worktree change counts from git status.
- `uv run litehive worktree clean --workspace .`
  Removes Litehive-managed worktrees for closed tasks (`done`, `deferred`, `wont_do`, `duplicate`) while protecting the active task worktree.
- `uv run litehive worktree clean --workspace . --dry-run`
  Shows which closed-task worktrees would be removed without deleting worktrees or mutating task metadata.
- `uv run litehive dirty-worktree-gate --workspace .`
  Explains whether dirty git state should block the workspace and whether each dirty location belongs to main or a tracked task worktree.
- `uv run litehive rollback T-0002 --workspace .`
  Reverts the task checkpoint commit and requeues the task.
- `uv run litehive recover T-0002 --workspace .`
  Requeues a completed task without reverting repository code.
- `uv run litehive report --verdict pass --role swe --step implementing --message "..." --workspace .`
  Records the stage verdict that drives the next routing decision.
- `uv run litehive daemon run --workspace .`
  Starts the background daemon. Each iteration repairs first, runs one fresh `litehive run`, writes logs under `.litehive/logs/run-all/`, and stops on an explicit pool stop reason.
- `uv run litehive daemon status --workspace .`
  Shows the registered daemon PID plus the latest workspace-local run-all logs.
- `uv run litehive daemon stop --workspace .`
  Stops the workspace daemon cleanly.
- `uv run litehive daemon restart --workspace .`
  Restarts the workspace daemon.
- `uv run litehive daemon instances`
  Lists live daemons across workspaces from the global registry at `~/.config/litehive/daemons.yaml`.

## Engines And Routing
- Supported adapters: `codex`, `opencode`, `gemini`, `copilot`, `claude`, `goz`.
- Important model defaults: `opencode_model: zai-coding-plan/glm-5.1`, `goz_model: glm-5-turbo`, `claude_model: claude-sonnet-4-20250514`; most other model fields are optional overrides.
- Claude is intentionally opt-in through config; do not assume it is enabled in a workspace.
- Engine resolution precedence is: run override, task-level engine, `task_engine_routing` for the task type, then workspace `default_engine`.
- Task-type routing exists for categories such as `adapter`, `bugfix`, `research`, `review`, `refactor`, `docs`, and `intake`.
- `recovery_engine` can route recovery and merge-resolution agents to a different adapter than normal execution.
- Engine fallbacks are separate from task retry policy; they handle quota, usage-limit, or transient engine failures inside a single stage attempt.
- Continuation metadata is captured where adapters support it so retries, interruptions, and engine switches can carry forward context.

## Pipeline And Modes
- Fixed stage flow: `backlog -> grooming -> implementing -> testing -> accepting -> commit_to_git -> done`.
- Shared owners: `planner` owns grooming, `swe` owns implementing, `qa` owns testing, `reviewer` owns acceptance, and `recovery` handles bounded repair when flagged or interrupted work re-enters a stage.
- `litehive run` is single-task mode: one selection cycle with tight operator control.
- `litehive run --drain` is full-pool mode: keep selecting tasks until an explicit stop condition such as queue exhaustion, blocked-only remainder, dirty git, human checkpoint, or configured pool limit.
- Task creation also has a separate mode distinction: `implementation` versus `tasks`. Typed intake defaults to `tasks` mode so the task folder gets richer shaping artifacts.
- Stage verdicts remain `pass`, `accept`, `fail`, `reject`, `blocked`, plus `comment` for reporting without advancing state.
- Review rejection is normal iteration, not terminal failure. `testing` or `accepting` rejection routes the task back to `implementing`.
- Tasks that cannot make progress right now should stay visible through states such as `interrupted`, `parked`, `flagged`, `wont_do`, `deferred`, or `duplicate`, rather than disappearing.
- Tasks may pause at human checkpoints before acceptance or commit; the pool stops cleanly and leaves the task queued at the next stage.

## Hooks, Recovery, And Daemon Behavior
- Runner hooks execute around implementation and acceptance boundaries.
- Supported hook points: `before_swe_implementation`, `after_swe_implementation`, `before_pm_acceptance`, `after_pm_acceptance`.
- Hooks carry a shell command plus a `blocking` flag; `pre_acceptance_command` is legacy sugar that folds into `before_pm_acceptance` as a blocking hook.
- Recovery is intentionally layered: repair workspace state first, resume or requeue tasks second, then launch recovery or merge-resolution agents when a bounded fix is possible.
- Recovery agents start from task-local evidence such as the latest stage report, `runtime.yaml`, `thread.yaml`, `events.jsonl`, subagent artifacts, and recent daemon logs.
- Merge-resolution agents are used for rebase/merge conflicts during `commit_to_git` and other bounded integration repair flows.
- If a task is flagged or system-interrupted and re-enters `implementing`, `testing`, or `accepting`, Litehive can route that stage through the `recovery` role instead of the normal owner.
- The orchestrator remains the manager: routing is deterministic and local, while subagents execute assigned stages without self-routing.

## Workspace Layout
- Litehive keeps workspace state under `.litehive/`.
- Main durable workspace files: `.litehive/.gitignore`, `.litehive/config.yaml`, `.litehive/context.md`, `.litehive/state.yaml`, and task folders under `.litehive/tasks/`.
- Important top-level runtime paths: `.litehive/logs/`, `.litehive/worktrees/`, `.litehive/pool-summary.txt`, `.litehive/engine-monitoring.yaml`.
- Typical task directory contents include `task.yaml`, `brief.md`, `runtime.yaml`, `journal.md`, `thread.yaml`, `events.jsonl`, `reports/`, `recovery/`, and `subagents/`.
- Durable task source of truth: `task.yaml` plus tracked stage reports, journal/thread history, and recovery reports.
- Runtime-only task state lives mainly in `runtime.yaml`, active worktree metadata, live subagent/session artifacts, and daemon or pool logs.
- Task worktrees under `.litehive/worktrees/` are intentionally ignored because they are ephemeral execution sandboxes rather than durable project state.
- `commit_to_git` generally commits the durable `.litehive/` metadata, but ignored runtime-only churn should not force meaningless checkpoint commits.

## Working Rules
- Keep changes scoped to the current task.
- Prefer targeted tests over broad suites.
- Treat `.litehive/` YAML and task artifacts as the source of truth; prompts and transcripts are supporting evidence.
- Start from `task.yaml`, the latest report, the latest recovery artifact, and `thread.yaml` before broad repo exploration.
- If you add or change operator-facing workflow, routing, or commands, update the durable docs and this context so later runs inherit the new state.
