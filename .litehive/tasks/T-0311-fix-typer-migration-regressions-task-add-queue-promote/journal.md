# T-0311 Fix Typer migration regressions — task add and queue promote are broken

## 2026-04-10T14:50:15+00:00
Task created immediately after T-0263 (Typer migration) landed. Two regressions discovered within minutes of landing: `task add` drops all options silently, `queue promote` crashes with AttributeError on a non-existent typer API. Both are blockers for normal workspace operation.

## 2026-04-10T15:18:15+00:00
Created task worktree at `.litehive/worktrees/T-0311-fix-typer-migration-regressions-task-add-queue-promote`.

## 2026-04-10T15:18:15+00:00
Execution started with engine `codex`.

## 2026-04-10T15:25:23+00:00
Runner hook `after_implementing` passed: `uv run ruff check --select E402,F401 litehive tests`.
- step: `implementing`
- reject_on_failure: `True`
- description: `-`
- exit_code: `0`
- artifact: `artifacts/after_implementing-001.yaml`

## 2026-04-10T15:25:23+00:00
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
