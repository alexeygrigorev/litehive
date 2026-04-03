# litehive

Local-first autonomous coding workspace with deterministic task execution.

litehive is about making agent-driven development deterministic and system-controlled.
The goal is to let agents perform at their best while constraining them to a clear process:
the runner owns state transitions, routing, and git integration so agents cannot freely invent
workflow state or skip required steps. In practice, the restriction is a feature. Agents tend
to perform better when the process is explicit, validated, and enforced.

The intended operating mode is simple:

- keep a working queue of tasks
- run the queue in order
- let the system continue until there is no runnable work left
- allow agents to add follow-up work to the queue while the project is in progress
- keep execution, verification, acceptance, and integration under runner control

## Current model

- Single active task at a time
- Local YAML-backed workspace state
- Two modes: `tasks` and `implementation`
- Deterministic stage pipeline
- Subagents executed through external CLIs (`codex`, `opencode`, `gemini`, and `copilot`)
- Checkpoint commit after successful task completion
- Queue state, runtime state, and task artifacts stored under `.litehive/`
- `litehive run` drains the active and queued pool, re-reading queue state between tasks
- `litehive run --dry-run` previews the planned pool order, engine selection, and predicted stop reason without invoking any agents
- Optional per-task human checkpoints can pause the pool before `accepting` or `commit_to_git`

## CLI workflow

Common commands:

- `litehive configure`
- `litehive status`
- `litehive queue`
- `litehive add "<title>"`
- `litehive add "<title>" --pm-complexity moderate --planned-effort m`
- `litehive add "<title>" --task-type review`
- `litehive update T-0001 --engine opencode`
- `litehive update T-0001 --pm-complexity complex --planned-effort l`
- `litehive move T-0001 1`
- `litehive prioritize T-0003 T-0002 T-0001`
- `litehive promote T-0001`
- `litehive requeue T-0001 --front`
- `litehive run`
- `litehive run --dry-run`
- `litehive rollback T-0001`
- `litehive recover T-0001`

`--workspace` defaults to the current directory. In normal repo-local use you should not need to pass it.
When `litehive add` receives `--task-type`, it now creates the task in `tasks` mode by default so the task folder includes the structured `brief.md` and prompt guidance for that template. Pass `--mode implementation` to keep a typed task on the implementation path without the intake brief.
Tasks can also carry PM sizing metadata: `--pm-complexity simple|moderate|complex` and `--planned-effort xs|s|m|l|xl`.
During grooming, the planner can emit `PM_COMPLEXITY:` and `PLANNED_EFFORT:` lines and litehive will persist them back into the task record and brief.
Configuration now layers built-in defaults, then `~/.config/litehive/config.yaml` (or `$XDG_CONFIG_HOME/litehive/config.yaml`), then workspace-local `.litehive/config.yaml`. Workspace settings take precedence over the global file.

## Execution model

Each runnable task goes through a fixed stage pipeline:

1. `grooming`
2. `implementing`
3. `testing`
4. `accepting`
5. `commit_to_git`

The orchestrator owns routing and task state. Subagents produce reports and artifacts, but they do not decide the control flow.
Agents are meant to operate inside this system rather than around it: they implement, verify, and report, while litehive enforces the stage order, validates results, and owns final integration.
`planner` owns `grooming` and `reviewer` owns `accepting`; both are PM-style roles with different prompts and success criteria.
Tasks can also opt into `--human-checkpoint before_acceptance` or `--human-checkpoint before_commit`, which pauses the pool and requeues the task at the next stage boundary for manual review.
External engine choice resolves as:

1. run-time override
2. task-level preference
3. workspace default engine

The full state machine — states, transitions, verdicts, outcome codes, and the
change-gate rule — is documented in [`docs/state-machine.md`](docs/state-machine.md).

In continuous operation, litehive should be able to keep draining the pool, pick up newly queued work between iterations, and stop only when there is no active or queued runnable task left or when an explicit stop condition is hit.

## Workspace shape

```text
.litehive/
  config.yaml
  context.md
  state.yaml
  tasks/
    T-0001-example/
      task.yaml
      brief.md
      journal.md
      subagents/
      reports/
      artifacts/
```

Use `.litehive/context.md` to describe the repo, commands, and workflow conventions that every future subagent run should inherit.
`litehive configure` accepts `--process-profile` so new workspaces start from a shared process scaffold plus a project-specific overlay.
The shared scaffold captures stages, orchestrator routing, issue/task source of truth, role model, TDD expectations, verification discipline, acceptance flow, and commit/recovery policy.
Built-in overlays currently include `generic`, `python`, `django`, `rust`, and `codehive`, and the generated context now records both the init scaffold and the prompt scaffold used for stage prompts.

## Observability

`litehive status` shows:

- active task
- queue size
- current stage
- PM complexity and planned effort when present
- explicit close outcomes such as `wont_do`, `deferred`, and `duplicate`
- live subagent role and engine
- latest report summary
- persisted rationale and follow-up task linkage for closed work
- retry policy details
- recent checkpoint commit for completed tasks

Task-local artifacts live under `.litehive/tasks/<task-id>/` and include reports, transcripts, prompts, journals, and subagent sessions.

## Git checkpoints

By default, litehive records a git completion commit whenever a task reaches `done` and the workspace is a git repository.
The task stores the checkpoint policy in `task.yaml`, including the commit subject, the base `HEAD`, and the number
of completed attempts.

- Default checkpoint subject: `litehive: complete <task-id> <slug>`
- Repeat completion attempts keep the same generated subject and append an attempt suffix: `litehive: complete <task-id> <slug> (attempt N)`
- The checkpoint happens at `commit_to_git`; a task only reaches and stays `done` after that checkpoint is recorded unless task-level or workspace-level auto-commit is disabled
- `litehive rollback <task-id>` preserves the attempt counter, reverts the recorded checkpoint into a new rollback commit, and requeues the task at the implementation entry stage
- `litehive recover <task-id>` clears the recorded checkpoint pointer without reverting code, keeps the attempt counter, and requeues the task at the implementation entry stage
- The implementation entry stage is normally `implementing`; Litehive reroutes recovery to `grooming` instead when structured acceptance criteria are still required.

Rollback and recover are only valid for completed tasks. Rollback also requires a clean git worktree so the revert is deterministic.

## Run-All Wrapper

Use [`scripts/run-all.sh`](/home/alexey/git/litehive/scripts/run-all.sh) to restart `litehive` on every pool iteration:

```bash
scripts/run-all.sh .
```

It writes timestamped logs under `.litehive/logs/run-all/<timestamp>/`:

- `0001-pre-status.log`
- `0001-run.log`
- `0001-post-status.log`

That keeps the pool inspectable and ensures each iteration picks up the latest `litehive` code.

Use [`scripts/run-all-status.sh`](/home/alexey/git/litehive/scripts/run-all-status.sh) to inspect the live workspace plus the latest run-all logs in one place:

```bash
scripts/run-all-status.sh .
```

## Local launcher

Install a `~/bin/litehive` launcher for this repo with:

```bash
scripts/install-bin.sh
```

The script:

- writes launchers into `~/bin`
- checks that `~/bin` is on `PATH`
- prints the resolved command paths

For this repository, the launcher delegates to the local project via `uv`, but that is an implementation detail. User-facing workflow should treat `litehive` as the command.

Installed commands:

- `litehive`
- `litehive-run-all`
- `litehive-run-all-status`

From any other project, run:

```bash
litehive-run-all /path/to/project
litehive-run-all-status /path/to/project
```

If you are already in the target project directory, you can omit the path:

```bash
litehive-run-all .
litehive-run-all-status .
```

## Current engines

Currently implemented adapters:

- `codex`
- `opencode`
- `gemini`
- `copilot`
- `claude`

Claude support is implemented in-tree but remains opt-in. Set `claude_enabled: true`
in `.litehive/config.yaml` to allow task routing or explicit engine selection to use it.
