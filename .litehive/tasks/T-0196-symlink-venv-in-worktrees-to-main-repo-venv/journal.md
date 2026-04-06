# T-0196 Symlink .venv in worktrees to main repo venv

## 2026-04-06T20:20:03+00:00
Task created.

## 2026-04-06T20:20:54+00:00
Task closed: wont_do. Symlinked venv is unsafe - agents may install packages that affect other worktrees. Separate venvs per worktree is safer, disk cost is small.
