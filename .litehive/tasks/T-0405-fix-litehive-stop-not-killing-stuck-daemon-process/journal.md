# T-0405 Fix litehive stop not killing stuck daemon process

## 2026-04-17T07:07:12+00:00
Task created.

## 2026-04-17T07:17:42+00:00
Task metadata updated via CLI.

## 2026-04-17T07:45:23+00:00
Interrupted subagent execution while `testing` was running. Reason: Stale runner detected while subagent `SA-0005` (qa/codex, pid 2234807 no longer alive) was still marked running in `testing`.. Subagent `SA-0005` (qa/codex, pid=2234807, path `subagents/SA-0005-qa`) stopped with status `interrupted`. Last snippet: REJECT: `litehive start` still fails to recover a stuck live PID when `.daemon.lock` has no `heartbeat_at` field.. Resume from `testing`.
