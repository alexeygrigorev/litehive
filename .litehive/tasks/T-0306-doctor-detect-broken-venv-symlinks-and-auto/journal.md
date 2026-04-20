# T-0306 Doctor: detect broken .venv symlinks and auto-repair after uv cache clean

## 2026-04-10T13:18:31+00:00
Task created.

## 2026-04-10T13:19:16+00:00
Task closed: wont_do. Obsoleted by T-0302 sandbox enforcement. Once subagents run inside containers with isolated venvs, host uv cache clean cannot break anything the agent sees — the broken-symlink hazard exists only because we currently run hooks on the host directly.

## 2026-04-13T10:46:29+00:00
Task closed: duplicate. Zombie re-queue: runtime.yaml execution_status=cancelled. See T-0366.

## 2026-04-19T01:14:48+00:00
Task metadata updated via CLI.
