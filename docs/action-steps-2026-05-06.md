# Action Steps - 2026-05-06 Voice Notes

Source files:

- `docs/source-recordings/google_recorder_litehive_3.openai-whisper-1.txt`
- `docs/source-recordings/google_recorder_litehive_4.openai-whisper-1.txt`
- `docs/source-recordings/google_recorder_litehive_5.openai-whisper-1.txt`
- `docs/source-recordings/google_recorder_litehive_6.openai-whisper-1.txt`

The Google Recorder transcripts for this batch are too noisy to use
as implementation input. Use the OpenAI Whisper transcripts above as
the working source, and use the `.m4a` files only when a phrase still
needs confirmation.

## A1. Raise The Docstring Standard Everywhere

Source: `google_recorder_litehive_3` 00:15-03:45,
`google_recorder_litehive_4` 02:53-03:07,
`google_recorder_litehive_6` 03:05-03:51.

1. Add a concrete docstring rule to `docs/code-style.md`.
2. Use multi-line docstrings for non-trivial helpers:
   opening triple quotes on their own line, body, closing triple
   quotes on their own line.
3. For helper/service docstrings, require three pieces of
   information:
   why the helper exists, which domain actor/service calls it, and
   what problem the helper solves.
4. Document parameters when their domain meaning is not obvious.
5. Stop adding docstrings that repeat the function name in prose
   (`write_stream_artifact` -> "writes stream artifact"). Replace
   them with caller/use-case information.
6. Remove Markdown-heavy formatting inside docstrings when it does
   not render in normal code reading. Prefer plain text and single
   backticks over double backticks.
7. Sweep existing changed/nearby docstrings first in artifacts,
   execution trace, sandbox support, domain/common, and runtime
   settings.

## A2. Replace Artifact Free Functions With A Domain Service

Source: `google_recorder_litehive_3` 01:05-03:45,
08:35-09:24; `google_recorder_litehive_4` 08:11-09:27.

1. Review `litehive/agents/artifacts.py` and all callers.
2. Replace path-first artifact helpers with an object such as
   `ArtifactWriter` or `ArtifactService` that receives its base path
   in the constructor.
3. Put stream artifacts, text artifacts, read/write behavior, and
   allowed format-flip cleanup behind that service.
4. Explain in docstrings who writes each artifact: subagent,
   session store, manager callback, or operator command.
5. Re-check whether `remove_text_artifact` is still needed. If it is
   only dead API surface, delete it with tests. If it is needed,
   document the caller and why deletion is valid.
6. Re-check whether `read_stream_artifacts` and
   `load_subagent_execution_trace` are still needed now that SQLite
   owns runtime state. Delete file-read paths that only preserve old
   `execution.trace.md` behavior.
7. Add tests for artifact writing, format switching, and session
   store integration before changing behavior.

## A3. Redesign Execution Trace Around A Timeline Object

Source: `google_recorder_litehive_3` 04:12-08:17.

1. Audit execution-trace helpers such as `parse_unified_events`,
   `event_stream_from_events`, `retry/dry events`, and
   `render_execution_trace`.
2. Replace unclear return types like `tuple[UnifiedEvent, ...]`
   with a named domain type. If immutability matters, make that type
   explicit instead of relying on tuple syntax.
3. Introduce or identify a `Timeline`/execution timeline domain
   object that owns event parsing, event ordering, and rendering to
   event stream form.
4. Delete fallback/retry-event plumbing if production does not use
   it. Do not keep speculative branches for non-existent sources.
5. Move Heru-specific rendering/parsing into the Heru/lifecycle
   package when it does not depend on task id or subagent id. Keep
   task/subagent-specific assembly near the agents/session boundary.
6. Rename `event_stream_from_events` to a name that states the
   domain operation, or remove it after the timeline object absorbs
   the behavior.
7. Add characterization tests for current trace rendering before the
   move, then migrate one helper at a time.

## A4. Split `AgentManager` And Move Role Defaults Into Domain

Source: `google_recorder_litehive_3` 09:27-11:02.

1. Split `litehive/agents/manager.py`; it is still too large.
2. Extract session/artifact/callback concerns into focused services
   that collaborate with the manager instead of subclassing it.
3. Move `DEFAULT_STAGES_FOR_ROLES` and similar dictionaries into
   domain behavior. A role should expose its default stage through a
   method/property, for example `role.default_stage`.
4. Search for similar dictionaries mapping domain concepts to other
   domain concepts. Replace them with object behavior or typed enum
   methods.
5. Investigate why file-change reporting can have
   `source_agent_id=None`. Either make the source explicit or
   document and type the system/operator-source case.
6. Add tests around manager session boundaries, default stage
   resolution, and file-change source attribution.

## A5. Strengthen Parsing And Domain Models

Source: `google_recorder_litehive_4` 00:01-01:23.

1. Revisit the previous `agents/parsing.py` feedback and verify each
   item is actually fixed.
2. Convert verdict strings such as reject/fail into domain enums at
   the boundary. Do not pass raw strings through parser internals.
3. Replace `failure_diagnostics` dictionaries with a typed
   `FailureDiagnostics` dataclass or domain model.
4. Move `last_task_activity_entry`-style helpers into the domain
   object that owns the behavior. If a task belongs to a workspace,
   the task/workspace service should expose that query instead of a
   loose free function.
5. Run a dedicated domain-model review across `litehive/domain/`,
   `litehive/agents/`, and `litehive/tasks/`. The goal is readability:
   fewer piles of free functions, more domain objects with clear
   ownership.

## A6. Move And Simplify Sandbox Support

Source: `google_recorder_litehive_4` 01:27-08:09.

1. Move sandbox code out of `litehive/agents/` into the sandbox
   package/module. `sandbox_support.py` and `sandbox.py` should not
   live under agents if they own sandbox behavior.
2. Remove `__all__` export bags that reappeared in sandbox support.
3. Fix sandbox docstrings, especially summary/as-dict helpers, so
   callers and reasons are clear.
4. Replace `as_dict` plumbing with dataclasses or typed config
   objects unless serialization at the boundary truly requires a
   dict.
5. Simplify policy selection. The desired rule from the note is:
   sandbox is globally enabled or disabled by config; do not maintain
   per-engine sandbox policies unless a real product need exists.
6. Push defaults into config loading. `SandboxLauncher` should
   receive a resolved policy and should not contain fallback/default
   branches for unset config.
7. Separate generic sandbox concepts from Docker-specific behavior.
   Keep Docker wrapping in a `DockerSandboxLauncher` or similarly
   named implementation, with only real shared behavior in the base
   abstraction.
8. Remove `unset`/plain `object` sentinel usage. Use concrete
   dataclasses and explicit optional fields only where absence is a
   real domain state.
9. Add tests for enabled/disabled sandbox config, resolved policy,
   Docker command wrapping, and launch failure diagnostics.

## A7. Make Subagent Session/Artifact Saving Domain-Owned

Source: `google_recorder_litehive_4` 08:11-10:47.

1. Model a subagent as belonging to a workspace and a task.
2. Give that subagent/session object enough information to save its
   own artifacts: session, report, event stream, role, and task id.
3. Replace scattered `save_subagent_artifacts` free-function calls
   with a method or service call on that domain object.
4. If session report or event stream can be absent, model that as a
   separate explicit state; do not use an untyped unset sentinel.
5. Audit functions where `workspace` is the first argument. For each,
   decide whether it should become a method on `Workspace`, `Task`, or
   a focused workspace service.
6. Review the session module, which is still large. Split activity
   timeout and compiled inactivity-pattern behavior into clearer
   units if they remain necessary.
7. Turn every small repeated note from the previous recordings into a
   separate task before implementing the broad refactor.

## A8. Clean Workspace, Paths, Registry, And Runtime Settings

Source: `google_recorder_litehive_5` 00:01-10:50.

1. Move config paths that are really workspace responsibilities onto
   `Workspace` or a workspace service. The container should resolve
   LiteHive root once, create `Workspace`, and then get paths from
   that object.
2. Remove registry global/function-as-global state. Use dependency
   injection for registry access.
3. Split registry responsibilities: config, persistence queries,
   security checks, mutex/busy-timeout/retry policy, and workspace
   path registration should not all live in one mixed module.
4. Audit runtime settings for duplicated config-layer code. Remove
   repeated `read_config_layer` / `merge_config_layers` behavior if
   config already owns it.
5. Replace hand-written JSON dump/load helpers with typed model
   serialization where practical. Values and contexts should be
   normal objects or strings, not broad `Any` mappings.
6. Remove wrapper functions such as `set_engine_preference` if they
   only call `set_runtime_setting` without adding domain behavior.
7. Simplify `clear_engine_freeze`: it should do the smallest database
   mutation/audit entry needed, not a large side-effect flow.
8. Remove unnecessary default parameters where callers always pass
   values. Required values should be explicit at every call site.
9. Delete workspace-parent-root discovery and similar upward search
   behavior. Loading a workspace from a non-LiteHive project should
   raise a clear error; bootstrap/create is the only path that may
   create workspace state.
10. Remove task-existence helpers such as "task is none or task
    exists" if they obscure the real branches. Closed/done/resumable
    task handling should be explicit.
11. Simplify complex list comprehensions and path-inside-managed-
    workspace checks so the intent is readable at a glance.

## A9. Clarify Agent And Common Domain Types

Source: `google_recorder_litehive_6` 00:01-06:17.

1. Review `litehive/domain/agent.py`. Explain what `subagent_ref`
   means or replace it with a clearer type/name.
2. Change `execution_trace` from a single string to a structured list
   if it represents many agent actions.
3. Remove optional `None` fields where the value is always required,
   especially continuation/contextual fields. Keep optionality only
   for real failure-only states.
4. Move generic helpers such as `utc_now`, feedback truncation, and
   `cap_feedback` out of `domain/common.py` if they are utilities
   rather than domain concepts.
5. Split `domain/common.py` if section comments are compensating for
   too many unrelated concepts in one file.
6. Consider renaming `OutcomeKind` to `TaskOutcomeKind` for clarity.
7. Document the relationship between outcome kind, outcome reason
   code, and verdict. Include who can set each reason code and in
   what situation.
8. Remove duplicate values between verdict and outcome reason code
   enums when they represent the same concept.
9. Explain or delete reason codes whose domain meaning is unclear,
   especially `hallucinated_completion` and `blocked_on_follow_up`.
10. Add tests that assert supported verdicts/reason codes cannot
    drift into unsupported serialized values.

## A10. Execution Discipline

Source: all four notes.

1. Do not claim a note is fixed until the exact old code path has
   been checked. The recordings repeatedly point out previous
   comments still present in code.
2. Convert each small detail into a separate task before broad
   implementation.
3. Land characterization tests before structural refactors.
4. Keep commits small and green. Run `make test` for unit changes and
   `make test-integration` for sandbox, CLI round-trip, daemon, or
   engine-adapter behavior.
5. Do not edit lint, formatter, pyrefly, ruff, or CI settings unless
   the operator explicitly asks for that configuration change.
