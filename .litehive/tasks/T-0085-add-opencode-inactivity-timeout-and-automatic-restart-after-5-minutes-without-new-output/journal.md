
## 2026-04-13T01:08:51+00:00
Task record updated from grooming output:
- goal: `Detect when an OpenCode subagent stops producing new output for 5 minutes, terminate the stale subprocess, and automatically resume execution through Litehive's existing timeout retry/restart flow instead of leaving the task hung.`
- acceptance_criteria: `['OpenCode live executions enforce a 300-second inactivity watchdog based on absence of new stdout/output from the agent.', 'When the watchdog fires, Litehive terminates the stale OpenCode subprocess and records the failure as a retryable `timeout` classification.', 'A timed-out OpenCode stage automatically re-enters the existing retry/restart flow for the same task without manual operator action.', 'The change does not unintentionally alter inactivity behavior for non-OpenCode engines unless the implementation deliberately reuses shared timeout plumbing and preserves current behavior for them.', 'Tests cover both the stale-output timeout path and the automatic retry/restart behavior triggered by that timeout.']`
- constraints: `['Keep changes scoped to OpenCode inactivity handling and its existing retry/recovery wiring.', 'Prefer existing timeout classifications, retry budgets, and continuation/session plumbing over adding a parallel recovery mechanism.', 'Do not broaden this task into daemon restart UX or unrelated workspace-level recovery changes.']`
- plan: `['Inspect the OpenCode live execution path, current inactivity timeout handling, and where timeout-classified failures enter pipeline retry logic.', 'Add or override a 300-second inactivity budget for OpenCode runs keyed to lack of new stdout/output.', 'Ensure the timeout path kills the stale subprocess and surfaces as the existing `timeout` retry classification so the pipeline restarts automatically.', 'Add focused tests for OpenCode inactivity timeout behavior and for pipeline retry/restart after that timeout.']`
- pm_complexity: `moderate`
- planned_effort: `m`
- priority: `high`

## 2026-04-16T23:33:49+00:00
Task metadata updated via CLI.

## 2026-04-16T23:34:18+00:00
Task metadata updated via CLI.
