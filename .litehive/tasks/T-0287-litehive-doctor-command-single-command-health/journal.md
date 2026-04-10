# T-0287 litehive doctor command: single-command health check

## 2026-04-10T05:59:18+00:00
Task created.

## 2026-04-10T08:50:54+00:00
Created task worktree at `.litehive/worktrees/T-0287-litehive-doctor-command-single-command-health`.

## 2026-04-10T08:50:54+00:00
Execution started with engine `codex`.

## 2026-04-10T08:55:29+00:00
Task record updated from grooming output:
- constraints: `['Keep changes scoped to the new `doctor` command and shared detection/fix helpers it needs.', 'Prefer reusing existing `repair_workspace_state()`, worktree inspection, and task-loading utilities over reimplementing repair logic.', 'Treat malformed `state.yaml` as a first-class doctor finding; the command should diagnose it without crashing.', 'Do not make `--fix` delete worktrees, resolve git divergence, or mutate ambiguous flagged/merge-failed task state automatically.']`
- plan: `['Define the `doctor` command surface and output contract, including exit-code behavior for clean, warning, and fix-applied runs.', 'Extract current ad hoc health checks from `litehive/main.py` / related CLI code into reusable doctor detection helpers.', 'Implement detectors for each required finding class, including a safe parser path for malformed or conflict-marked `.litehive/state.yaml`.', 'Wire `--fix` only to deterministic non-destructive remediations, likely by delegating duplicate-ID and stranded-commit recovery to existing repair helpers and adding narrow helpers for any remaining safe cases.', 'Add focused CLI tests that construct each broken workspace condition and assert both the finding text and the recommended fix command.', 'Update CLI help/README so operators know when to use `doctor` versus `health` and `repair`.']`
- pm_complexity: `moderate`
- planned_effort: `m`

## 2026-04-10T09:09:33+00:00
Runner hook `after_implementing` passed: `uv run ruff check --select E402,F401 litehive tests`.
- step: `implementing`
- reject_on_failure: `True`
- description: `-`
- exit_code: `0`
- artifact: `artifacts/after_implementing-001.yaml`

## 2026-04-10T09:09:33+00:00
Runner hook `after_implementing` passed: `if [ -n "${LITEHIVE_CHANGED_PATHS:-}" ] \
  && printf '%s\n' "$LITEHIVE_CHANGED_PATHS" | grep -q . \
  && printf '%s\n' "$LITEHIVE_CHANGED_PATHS" \
    | xargs -r -d '\n' git grep -nE 'noqa:.*F401|noqa:.*F403|ruff:\s*noqa:\s*F401|ruff:\s*noqa:\s*F403' --; then
  echo 'Forbidden noqa F401/F403 suppression found.'
  exit 1
fi`.
- step: `implementing`
- reject_on_failure: `True`
- description: `-`
- exit_code: `0`
- artifact: `artifacts/after_implementing-002.yaml`

## 2026-04-10T09:12:07+00:00
Execution finished with status `queued`.

## 2026-04-10T09:12:43+00:00
Execution started with engine `codex`.

## 2026-04-10T09:20:31+00:00
Runner hook `after_implementing` passed: `uv run ruff check --select E402,F401 litehive tests`.
- step: `implementing`
- reject_on_failure: `True`
- description: `-`
- exit_code: `0`
- artifact: `artifacts/after_implementing-001.yaml`

## 2026-04-10T09:20:31+00:00
Runner hook `after_implementing` passed: `if [ -n "${LITEHIVE_CHANGED_PATHS:-}" ] \
  && printf '%s\n' "$LITEHIVE_CHANGED_PATHS" | grep -q . \
  && printf '%s\n' "$LITEHIVE_CHANGED_PATHS" \
    | xargs -r -d '\n' git grep -nE 'noqa:.*F401|noqa:.*F403|ruff:\s*noqa:\s*F401|ruff:\s*noqa:\s*F403' --; then
  echo 'Forbidden noqa F401/F403 suppression found.'
  exit 1
fi`.
- step: `implementing`
- reject_on_failure: `True`
- description: `-`
- exit_code: `0`
- artifact: `artifacts/after_implementing-002.yaml`
