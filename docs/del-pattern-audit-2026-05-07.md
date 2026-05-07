# `del ...` Pattern Audit - 2026-05-07

Source checklist item: `docs/voice-instructions-2026-05-06.md` SE5.

Command used:

```bash
rg "^\s*del [a-zA-Z_][a-zA-Z0-9_]*(,|$)|^\s*del [a-zA-Z_][a-zA-Z0-9_]*" litehive -g '*.py' -n
```

## Result

The session-specific deleted parameter that triggered this item
(`SubagentSessionManager.render_execution_trace(engine_name, ...)`) was
removed in `30046410`: trace rendering now lives in
`litehive/agents/execution_trace.py` and no longer accepts `engine_name`.

Remaining production `del ...` sites are outside `litehive/agents/session.py`
and fall into these groups:

- Lifecycle rule-table hooks whose signatures are dictated by the state
  machine (`state`, `event`, `trans`). These appear in
  `litehive/lifecycle/transitions.py`, `litehive/lifecycle/guards.py`,
  lifecycle nodes, and `litehive/domain/lifecycle_deltas.py`.
- Stub/null collaborators that intentionally satisfy a protocol while doing
  nothing, such as `_NullSelector` and `_NullSessions` in
  `litehive/lifecycle/heru_factory.py`.
- Signal or framework callback parameters that are part of an external API,
  such as `signum` in `litehive/daemon/execution.py`.
- Compatibility/display helpers that still accept an old argument shape, such
  as `SqliteReportReference.relative_to(root)`.
- A few migration candidates where a follow-up can remove or narrow the
  signature with focused tests:
  `litehive/recovery/execution_recovery.py::_can_skip_recovery_scan`,
  `litehive/attention.py::waiting_for_you_lines_for_workspace`,
  `litehive/observability/venv_health.py`, and task runtime/report helpers.

## Disposition

Do not remove the lifecycle hook parameters piecemeal: those signatures are
part of the rule-table or protocol contract. When a future slice touches one
of those owners, replace the broad hook shape with a narrower typed
collaborator or update the rule-table contract and tests together.

For new code, do not add `del unused_param` to silence a bad signature. If
the value is not required by an external protocol or hook contract, stop
passing it.
