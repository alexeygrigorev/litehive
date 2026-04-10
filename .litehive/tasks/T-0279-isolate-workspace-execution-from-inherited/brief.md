# T-0279 Isolate workspace execution from inherited operator VIRTUAL_ENV

- Mode: tasks
- Task type: bugfix
- PM complexity: moderate
- Planned effort: s

## Goal
Prevent Litehive child processes that execute in a task worktree or other target workspace from inheriting an unrelated operator-shell Python virtual environment.

## Acceptance Criteria
- When Litehive launches subagents or runner hooks against a task worktree or other execution root, the child process environment does not forward an unrelated parent VIRTUAL_ENV from the caller shell.
- Workspace-scoped helper commands continue to pass required Litehive metadata env vars, but uv resolves the target workspace environment instead of warning about the caller repo's active venv.
- Focused regression coverage demonstrates cross-workspace execution no longer emits the uv VIRTUAL_ENV mismatch warning in captured stderr/log artifacts.

## Constraints
- Prefer the smallest change that removes the failure mode.
- Call out any remaining edge cases or follow-up risk explicitly.

## Plan
- Localize every child-process launch path that starts from os.environ.copy() and can target a task worktree or alternate workspace execution root.
- Add a shared environment-sanitizing helper so subagent adapters, runner hooks, and daemon-launched workspace commands drop inherited VIRTUAL_ENV when executing outside the caller workspace while preserving required Litehive env vars.
- Add focused regression tests that simulate a parent-shell VIRTUAL_ENV and assert the child env/logs use the target workspace without uv mismatch warnings.
- Run the narrow affected pytest selection first, then any broader smoke command needed to prove the fix did not break normal workspace execution.

## PM Sizing
- Complexity: moderate
- Planned effort: s

## Template Guidance
- Describe the broken behavior, trigger, and expected correct behavior before changing code.
- Aim at root cause, not just the visible symptom.
- Include regression coverage or equivalent focused proof that the failure is gone.

## Intake Notes

### Bug and Reproduction
- Describe the failing behavior, trigger, and expected result.

Litehive child processes inherited the operator shell's active `VIRTUAL_ENV` even when the child executed in a task worktree or another workspace checkout. The trigger was any subagent launch, runner hook, or daemon-launched workspace command started from a different execution root than the caller checkout. The expected behavior is that cross-workspace child processes should resolve the target workspace environment and should not emit uv mismatch warnings about the parent repo's venv.

### Root Cause
- Note the suspected or confirmed cause in the affected path.

Confirmed cause: child-process env construction copied `os.environ` directly in multiple launch paths and forwarded `VIRTUAL_ENV` unchanged. The affected paths were `litehive/agents/base.py`, `litehive/agents/adapters/claude.py`, `litehive/pipeline/_hooks.py`, and `litehive/daemon/_execution.py`, which all launched children against task worktrees or alternate workspaces without sanitizing inherited venv state.

### Regression Coverage
- Record the exact test or check that prevents recurrence.

- `uv run pytest -q tests/test_engine_variants_and_timeline.py -k 'virtual_env or build_invocation'`
- `uv run pytest -q tests/test_task_commands_and_daemon.py -k 'virtual_env or daemon_loop_workspace_commands_do_not_log_inherited_virtual_env_warning'`
- `uv run ruff check --select E402,F401 litehive tests`
