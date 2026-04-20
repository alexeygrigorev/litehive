# T-0431 Make SWE prompts explicit for first attempt vs rejection retry

## 2026-04-17T15:27:26+00:00
Task created.

## 2026-04-20T11:48:22+00:00
Task metadata updated via CLI.

## 2026-04-20T11:50:53+00:00
Interrupted subagent execution while `grooming` was running. Reason: Stale runner detected while subagent `SA-0001` (planner/codex, pid 993660 no longer alive) was still marked running in `grooming`.. Subagent `SA-0001` (planner/codex, pid=993660, path `subagents/SA-0001-planner`) stopped with status `interrupted`. Last snippet: Rewrote T-0431 goal, acceptance criteria, plan, and constraints. Scope is now explicit: refactor retry-capable prompt construction so swe, qa, reviewer, merge-resolver, and recovery expose clearly named fresh versus retry prompt variants selected from last_rejection in the role or base prompt-construction path, not through one INSTRUCTIONS block plus an implicit serializer append. Keep serialize_prompt focused on rendering the chosen variant and rejection context rather than being the only place that conveys retry behavior. Add focused tests for both fresh and retry paths so retry-only guidance appears when last_rejection is set and the fresh variant appears otherwise.. Resume from `grooming`.

## 2026-04-20T11:53:47+00:00
Task metadata updated via CLI.
