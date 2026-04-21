# T-0252 Remove backward-compat shim directories (engines/, runtime/, runner/)

## 2026-04-09T09:16:11+00:00
Task created.

## 2026-04-09T13:58:22+00:00
Created task worktree at `.litehive/worktrees/T-0252-remove-backward-compat-shim-directories-engines`.

## 2026-04-09T13:58:22+00:00
Execution started with engine `claude`.

## 2026-04-09T13:58:23+00:00
Stage `grooming` switched from `claude` to `codex` after claude usage limit reached (7d window at 96%, resets 2026-04-10T09:00:00.677751+00:00).

## 2026-04-09T14:00:38+00:00
Task metadata updated via CLI.

## 2026-04-09T14:11:01+00:00
Runner hook `before_pm_acceptance` passed: `uv run ruff check --select E402,F401 litehive tests`.
- step: `accepting`
- blocking: `True`
- description: `-`
- exit_code: `0`
- artifact: `artifacts/before_pm_acceptance-001.yaml`

## 2026-04-09T14:11:01+00:00
Runner hook `before_pm_acceptance` passed: `if [ -n "${LITEHIVE_CHANGED_PATHS:-}" ] \
  && printf '%s\n' "$LITEHIVE_CHANGED_PATHS" | grep -q . \
  && printf '%s\n' "$LITEHIVE_CHANGED_PATHS" \
    | xargs -r -d '\n' git grep -nE 'noqa:.*F401|noqa:.*F403|ruff:\s*noqa:\s*F401|ruff:\s*noqa:\s*F403' --; then
  echo 'Forbidden noqa F401/F403 suppression found.'
  exit 1
fi`.
- step: `accepting`
- blocking: `True`
- description: `-`
- exit_code: `0`
- artifact: `artifacts/before_pm_acceptance-002.yaml`

## 2026-04-09T14:14:33+00:00
Merge conflict on 2 file(s). Launching merge agent (attempt 1).

## 2026-04-09T14:15:32+00:00
CommitToGit complete. Commit: 4cdfc1254e8d4f054d8c4b9849445054768dcf6c

## 2026-04-13T10:30:15+00:00
Task closed: duplicate. Zombie re-queue: runtime.yaml execution_status=done. See T-0366.

## 2026-04-17T16:52:22+00:00
Task metadata updated via CLI.

## 2026-04-21T21:29:38+00:00
Task closed: wont_do. User does not want backwards-compatibility or legacy-migration work in Litehive backlog.
