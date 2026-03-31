# litehive

Local-first autonomous coding workspace with deterministic task execution.

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
- `litehive add "<title>" --task-type review`
- `litehive update T-0001 --engine opencode`
- `litehive move T-0001 1`
- `litehive promote T-0001`
- `litehive requeue T-0001 --front`
- `litehive run`
- `litehive run --dry-run`
- `litehive rollback T-0001`
- `litehive recover T-0001`

`--workspace` defaults to the current directory. In normal repo-local use you should not need to pass it.
When `litehive add` receives `--task-type`, it now creates the task in `tasks` mode by default so the task folder includes the structured `brief.md` and prompt guidance for that template. Pass `--mode implementation` to keep a typed task on the implementation path without the intake brief.

## Execution model

Each runnable task goes through a fixed stage pipeline:

1. `grooming`
2. `implementing`
3. `testing`
4. `accepting`
5. `commit_to_git`

The orchestrator owns routing and task state. Subagents produce reports and artifacts, but they do not decide the control flow.
Tasks can also opt into `--human-checkpoint before_acceptance` or `--human-checkpoint before_commit`, which pauses the pool and requeues the task at the next stage boundary for manual review.
External engine choice resolves as:

1. run-time override
2. task-level preference
3. workspace default engine

The full state machine — states, transitions, verdicts, outcome codes, and the
change-gate rule — is documented in [`docs/state-machine.md`](docs/state-machine.md).

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
- live subagent role and engine
- latest report summary
- retry policy details
- recent checkpoint commit for completed tasks

Task-local artifacts live under `.litehive/tasks/<task-id>/` and include reports, transcripts, prompts, journals, and subagent sessions.

## Git checkpoints

By default, Litehive records a git completion commit whenever a task reaches `done` and the workspace is a git repository.
The task stores the checkpoint policy in `task.yaml`, including the commit subject, the base `HEAD`, and the number
of completed attempts.

- Default checkpoint subject: `litehive: complete <task-id> <slug>`
- Repeat completion attempts append an attempt suffix: `litehive: complete <task-id> <slug> (attempt N)`
- `litehive rollback <task-id>` reverts the latest checkpoint commit, creates a rollback commit, and requeues the task at `implementing`
- `litehive recover <task-id>` requeues a completed task at `implementing` without reverting code

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

Planned later:

- `claude`
