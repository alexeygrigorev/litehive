# T-0370 Wire ~/.codex mount + CODEX_HOME into the sandbox so codex works under bwrap

## 2026-04-13T20:16:13+00:00
Task created.

## 2026-04-13T20:19:46+00:00
Task record updated from grooming output:
- goal: `Restore sandboxed Codex execution under external_engine_sandbox by mounting the real host Codex home into the sandbox and exposing it via CODEX_HOME, then prove a full sandboxed pipeline run completes before re-enabling sandbox by default.`
- acceptance_criteria: `["When external_engine_sandbox is enabled for codex, the sandbox bind-mounts the operator's ~/.codex directory read-only and the launched Codex process sees CODEX_HOME pointing at that mounted path.", 'A deterministic test covers the sandbox policy/env wiring for codex so regressions in extra_ro_binds or CODEX_HOME propagation fail fast without needing a live engine.', 'A sandboxed Codex integration path proves both `codex --version` and a CLI verdict/report flow succeed end-to-end instead of failing with `No such file or directory (os error 2)`.', 'A smoke test runs one full sandboxed pipeline task through planner, swe, and reviewer and confirms each stage reaches a verdict rather than crashing on missing Codex home state.', 'After the above passes, default config/templates/docs set external_engine_sandbox.enabled back to true while preserving per-workspace/operator opt-out.']`
- constraints: `['Keep the implementation scoped to the confirmed Codex regression and the minimal reusable sandbox-policy mechanism needed to fix it.', 'Claude may reuse the same mount/env helper only if the repo shows a concrete host-path requirement during implementation; do not broaden this task into speculative Copilot or multi-engine inventory work.', 'Prefer existing sandbox and integration test harnesses over inventing a new runner unless the current harness cannot express the full sandboxed pipeline smoke.']`
- plan: `['Inspect current sandbox policy resolution, environment allowlisting, and default workspace-config wiring for codex.', 'Add the smallest supported mechanism to declare engine-specific read-only home binds plus required environment variables for sandboxed external engines.', 'Wire codex to mount ~/.codex and set CODEX_HOME; extend the same mechanism to claude only if direct evidence in this repo shows it is required for the same failure class.', 'Add focused unit coverage for bind/env resolution and an integration/smoke path that proves sandboxed codex startup and verdict submission work end-to-end.', 'Re-enable external_engine_sandbox.enabled in the shipped defaults only after the sandboxed smoke passes.']`
- pm_complexity: `moderate`
- planned_effort: `m`
- task_type: `bugfix`
- priority: `high`

## 2026-04-13T20:45:14+00:00
Interrupted subagent execution while `backlog` was running. Reason: Task stopped via CLI. Subagent `SA-0002` (swe/codex, pid=3296025, path `subagents/SA-0002-swe`) stopped with status `interrupted`. Last snippet: [stderr]. Resume from `backlog`.

## 2026-04-13T20:45:20+00:00
Task closed: duplicate. Already landed manually in commit af6ea1bd (Sandbox: per-engine bind + HOME/CODEX_HOME setenv + integration tests). Codex/claude/copilot/opencode work sandboxed end-to-end; gemini + goz tracked under T-0371 (autodiscovery).

## 2026-04-17T13:24:08+00:00
Task closed: wont_do. Bubblewrap sandbox removed

## 2026-04-22T06:09:17+00:00
Task closed: wont_do. Bubblewrap sandbox removed; obsolete task
