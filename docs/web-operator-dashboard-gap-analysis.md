# Web Operator Dashboard Gap Analysis

## Current baseline

- There is no `litehive/web/` package in this checkout.
- Repository search found no browser templates, static assets, or HTTP route layer to extend.
- The current operator surface is CLI-first. The browser work is greenfield at the transport and UI layer, but the underlying task, queue, daemon, status, and engine domain operations already exist.

## CLI source of truth

| Feature area | Current source modules | Notes |
| --- | --- | --- |
| Task detail views | `litehive/cli/task_cli.py`, `litehive/domain/task.py`, `litehive/tasks/activity.py`, `litehive/tasks/reports.py`, `litehive/cli/task_logs_support.py` | `task show`, `task logs`, report history, recovery evidence, and subagent artifacts already have read paths. |
| Task actions | `litehive/cli/task_cli.py`, `litehive/tasks/status.py`, `litehive/state/records.py` | Task create, update, close, abandon, and stop behavior already exists behind CLI commands. |
| Queue management | `litehive/cli/queue_cli.py`, `litehive/tasks/queue.py`, `litehive/tasks/status.py` | The CLI already exposes move, promote, prioritize, requeue, resume, stop, and task-engine switch flows. |
| Live agent output streaming | `litehive/cli/task_logs_support.py`, `litehive/agents/session_store.py`, `litehive/tasks/paths.py` | Active transcript and stdout tailing already exist in filesystem and SQLite-backed artifact helpers. |
| Engine monitoring | `litehive/cli/workspace.py`, `litehive/observability/status_diagnostics.py`, `litehive/observability/engine_monitoring.py`, `litehive/cli/queue_cli.py` | Engine status, freeze controls, usage/quota data, and per-task engine switching already exist in non-web code. |
| Daemon control | `litehive/cli/daemon_cli.py`, `litehive/cli/runner.py`, `litehive/observability/status_diagnostics.py`, `litehive/state/locking.py` | Daemon lifecycle, stale-runner detection, and run-all log inspection are already modeled for CLI use. |

## Browser-ready read paths

- `litehive.observability.status_diagnostics.collect_status_snapshot()` already assembles workspace status, queue state, runner state, engine monitoring, and health issues into one read-only snapshot.
- `litehive.state.locking.runner_status_readonly()` is explicitly designed for non-blocking consumers such as a web dashboard.
- `litehive.tasks.activity.load_task_activity()` and `litehive.tasks.reports.collect_recovery_evidence()` already expose the data needed for task history, report, and recovery sections.
- `litehive.cli.task_logs_support` and `litehive.agents.session_store` already provide the primitives needed for transcript and stdout views plus follow-mode style streaming.

## Gap summary

- The missing piece is not task logic. The missing piece is the entire browser layer: server entrypoint, route definitions, serialization, templates or assets, and live update transport.
- Because there is no existing HTTP layer, the first browser task must bootstrap a read-only web shell before any mutation or streaming work can land cleanly.
- Historical browser-task records `T-0221` through `T-0227` assume an existing `litehive/web` package and pre-existing endpoints. They do not match this checkout's greenfield baseline and should be treated as historical context only.

## Follow-up task split for this checkout

| Task | Scope |
| --- | --- |
| `T-0423` | Bootstrap the web package, read-only server shell, workspace snapshot endpoints, and task detail views. |
| `T-0424` | Add browser task actions for create, update, close, requeue, abandon, and stop-active-task. |
| `T-0425` | Add queue inspection and queue management actions for move, promote, prioritize, and resume. |
| `T-0426` | Add live browser streaming for workspace changes and active subagent output. |
| `T-0427` | Add engine monitoring, freeze controls, and per-task engine switching in the browser. |
| `T-0428` | Add daemon status, lifecycle controls, and run-all log viewing in the browser. |

## Explicit non-goals for this split

- This six-task rebaseline focuses on the requested operator areas and the missing browser foundation.
- Other CLI surfaces such as archive, backup, database administration, worktree rescue, repair or doctor, pipeline administration, and browser verdict submission remain separate follow-on planning concerns if full parity is still required later.
