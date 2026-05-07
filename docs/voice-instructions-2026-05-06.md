# Voice Instructions - 2026-05-06 Verification Checklist

This file extracts the concrete instructions from the 2026-05-06
voice notes so they can be checked one by one later. Use the OpenAI
Whisper transcripts as the working source:

- `docs/source-recordings/google_recorder_litehive_3.openai-whisper-1.txt`
- `docs/source-recordings/google_recorder_litehive_4.openai-whisper-1.txt`
- `docs/source-recordings/google_recorder_litehive_5.openai-whisper-1.txt`
- `docs/source-recordings/google_recorder_litehive_6.openai-whisper-1.txt`

Legend:

- `[ ]` not checked or not done.
- `[x]` verified done.
- `Audio check` means the transcript wording was unclear enough that
  the audio should be replayed before implementation.

## Global Rules To Record In `docs/code-style.md`

- [x] G1. Add a docstring-format rule: non-trivial docstrings should
  be written as opening triple quotes on their own line, body on the
  following lines, and closing triple quotes on their own line.
  Source: note 3, 00:15-00:44.
- [x] G2. Add a docstring-content rule: docstrings must explain why
  the helper exists, which actor/service calls it, and what problem
  it solves. They must not merely repeat the function name.
  Source: note 3, 00:50-02:04.
- [x] G3. Add a parameter-documentation rule for helpers whose
  parameters are not domain-obvious. The docstring must explain
  confusing parameters such as `source`.
  Source: note 3, 03:08-03:15; note 5, 11:35-11:50.
- [x] G4. Add a rule against Markdown-heavy docstrings that do not
  render while reading code. Avoid `**bold**` and double backticks in
  docstrings; prefer plain text and single backticks.
  Source: note 3, 10:41-10:47; note 6, 03:05-03:51.
- [x] G5. Add a rule that docstrings and prose should wrap around 80
  characters where practical, especially docstrings.
  Source: note 4, 35:23-35:42.
- [x] G6. Add or strengthen the rule against bare `*` keyword-only
  markers in function signatures. The notes explicitly call out a
  `*` in `MissingVerdictError.__init__` and ask to remove all such
  stars from parameters.
  Source: note 3, 32:19-32:48.
- [x] G7. Add SOLID / Single Responsibility guidance: when a class
  has unclear responsibility or too many concerns, split it into
  focused collaborators with explicit ownership.
  Source: note 3, 19:35-20:09; note 3, 32:48-33:08.
- [x] G8. Add the no-mixins rule: do not use mixins; use delegation
  and composition instead of inheritance.
  Source: note 3, 13:03-14:08; note 4, 10:48-11:13.
- [x] G9. Add or reinforce "composition over inheritance" and
  "dependency injection over hidden construction".
  Source: note 3, 13:03-14:43; note 5, 00:35-01:14.
- [x] G10. Add a function-size guideline: if a function does not fit
  on one screen, use 25 lines as the review threshold and split it
  into focused helpers.
  Source: note 4, 20:10-20:36.
- [x] G11. Add a rule that functions with four or more parameters
  should be reviewed. Prefer a domain object when the grouped values
  have a real domain meaning.
  Source: note 3, 24:08-24:47.
- [x] G12. Add a rule for `isinstance`: every `isinstance` should be
  reviewed. Remove it when better domain types can avoid it; if it
  must remain, add a comment explaining why.
  Source: note 3, 25:55-26:37.
- [x] G13. Add a rule for `getattr`: `getattr` is considered an
  antipattern in this codebase. Configure ruff or a custom grep check
  to fail if `getattr` appears in production code, unless an explicit
  exception is documented.
  Source: note 4, 39:30-40:58.
- [x] G14. Add a rule against untyped `object` parameters and plain
  object sentinels. They indicate missing domain types.
  Source: note 4, 07:55-08:23; note 4, 40:00-40:21.
- [x] G15. Add a rule to audit optional `None` fields honestly.
  Remove `None` where the value should always exist; keep optionality
  only for real domain states.
  Source: note 3, 11:03-11:53; note 3, 22:35-23:38; note 6,
  00:39-01:20.
- [x] G16. Add a rule against free functions that only call one other
  function and add no domain behavior. Delete them or move the real
  behavior to the caller/domain object.
  Source: note 5, 19:53-20:07.
- [x] G17. Add a rule against business logic in CLI modules. CLI code
  parses user input and dispatches to domain/container services.
  Source: note 4, 12:53-14:07; note 4, 16:57-19:09.
- [x] G18. Add a rule that environment reads such as `os.environ.get`
  belong only in DI/container/config boundary code.
  Source: note 4, 16:57-18:06.
- [x] G19. Add a rule that dictionaries returned from business logic
  should generally become dataclasses/domain objects.
  Source: repeated throughout note 3, note 4, and note 5.
- [x] G20. Add a rule that tuples returned from business logic should
  be reviewed. A returned tuple often means a named dataclass is
  missing.
  Source: note 3, 04:08-04:54; note 4, 30:35-30:53; note 4,
  41:17-41:40.
- [x] G21. Add a rule that mappings/dictionaries in config/profile
  parsing should be replaced by validated typed models where possible,
  preferably Pydantic if that is the project direction.
  Source: note 4, 33:30-36:08; note 4, 47:21-48:13.

## Artifact And Execution Trace Instructions

- [x] A1. Review all artifact helpers and their docstrings. Each must
  say who calls it and why it exists.
  Source: note 3, 01:05-03:45.
- [x] A2. Replace `base_path`-style free functions with an object that
  receives `Path` in its constructor, such as `ArtifactWriter` or
  `ArtifactService`.
  Source: note 3, 02:15-02:45.
- [x] A3. Put stream-artifact and text-artifact writing behind that
  service so the behavior is encapsulated in one class.
  Source: note 3, 02:22-02:35.
- [x] A4. Make the service name match the project domain language and
  existing service patterns.
  Source: note 3, 02:41-02:52.
- [x] A5. Re-check `remove_text_artifact`. If it is not needed,
  delete it. If it is needed, document exactly who calls it and why
  deletion is valid.
  Source: note 3, 03:29-03:47.
- [x] A6. Review `parse_unified_events`. Its
  `tuple[UnifiedEvent, ...]` return type is not readable enough.
  Replace with a named type, a clear immutable collection type, or a
  domain object.
  Source: note 3, 04:08-04:54.
- [x] A7. Decide which execution-trace helpers belong in Heru versus
  agent/session code. Heru-specific parsing/rendering should move to
  Heru; task/subagent assembly can stay near agents.
  Source: note 3, 05:07-06:07.
  Decision: Heru remains responsible for raw engine timeline
  extraction and transcript rendering. LiteHive keeps
  `litehive.agents.execution_trace` as the task/subagent adapter
  because it combines Heru events with SQLite session rows,
  runtime-state snippets, stdout/stderr artifacts, and task paths.
  No code move is justified until a helper stops depending on
  LiteHive task/subagent state.
- [x] A8. Rename or remove `event_stream_from_events`; the current
  name is unclear.
  Source: note 3, 06:27-06:35.
- [x] A9. Review `render_live_events` or similarly named live-event
  helper. It lacks a clear event type and source. Add types or move
  behavior to a timeline object.
  Source: note 3, 06:35-07:11.
  Verified: the current equivalent is
  `recovered_timeline_from_events`, which now accepts
  `ParsedUnifiedEvents` and documents that the source is events
  recovered from stdout when no structured live timeline was captured.
- [x] A10. Introduce a timeline object if appropriate. The timeline
  should own event collection, event ordering, and event stream
  rendering.
  Source: note 3, 07:20-07:46.
  Decision: do not introduce another timeline type yet. Heru's
  `LiveTimeline` is the timeline object for live/recovered engine
  events; LiteHive's `ParsedUnifiedEvents` is only a narrow recovered
  stdout wrapper used to build that timeline or render a trace.
- [x] A11. Delete fallback/retry-event paths if production does not
  use them.
  Source: note 3, 06:40-06:55.
  Verified: the fallback path is still used when the structured
  session-store event stream is unavailable for older, partial, or
  crashed runs. It is now named as recovered stdout behavior instead
  of a generic fallback event stream.
- [x] A12. Review `render_execution_trace`; likely move Heru-specific
  rendering out of agent code.
  Source: note 3, 07:46-08:10.
  Decision: leave the current LiteHive renderer in
  `litehive.agents.execution_trace` because it composes Heru events
  with LiteHive stderr, session-store payloads, runtime snippets, and
  task/subagent artifact paths. Pure engine transcript extraction
  stays in Heru.
- [x] A13. Delete `execution.trace.md` loading if SQLite is now the
  source of truth and the file is legacy.
  Source: note 3, 08:10-08:45.
  Verified with tests: `load_subagent_execution_trace` now prefers
  SQLite `subagent_sessions:event_stream` over cached
  `execution_trace.md`. The file read remains only as a legacy
  fallback for completed artifact-only runs that lack structured
  session events.
- [x] A14. Review `read_stream_artifacts`. If only legacy/recovery
  file reads use it, remove it. If it stays, explain in the docstring
  why reads are still needed.
  Source: note 3, 08:49-09:22.
  Verified: the current equivalent is `_read_stream_artifact`, used
  by `load_subagent_execution_trace` to read live `.log` files for
  active subagents and persisted `.txt` files for completed subagents
  when no structured event stream can provide a trace.

## Agent Manager, Sessions, And Subagents

- [x] M1. Split `litehive/agents/manager.py`; it is still too large.
  Source: note 3, 09:22-09:29.
- Verified 2026-05-06: first manager split landed by moving role/stage
  selection out of `litehive/agents/manager.py` into
  `litehive/domain/roles.py`; manager no longer owns
  `_DEFAULT_STAGE_FOR_ROLE`, `_agent_stage_for_task`, or
  `_report_stage_for_task`.
- [x] M2. Move `DEFAULT_STAGES_FOR_ROLES` and all similar role/stage
  dictionaries into domain behavior. A role object should know its
  default stage.
  Source: note 3, 09:31-10:30.
- Verified 2026-05-06: `AgentRole.default_stage` now owns the known
  planner/SWE/QA/reviewer/merge-resolver/recovery defaults without a
  manager-side dictionary. Existing `TaskStage.owner_role` already
  lives in the domain layer and remains there.
- [x] M3. Find every dictionary mapping one domain concept to another
  and replace it with object-oriented domain behavior.
  Source: note 3, 10:12-10:30.
- Verified 2026-05-06: removed the `domain/common.py` lookup tables
  for `TaskStage.owner_role`, `PipelineState -> TaskStage`, and
  `PipelineState -> PipelineStatus`; those relationships now live on
  `TaskStage.owner_role`, `PipelineState.task_stage`, and
  `PipelineState.pipeline_status`. Removed the lifecycle retry-counter
  stage table; `TaskStage.retry_counter_state` now owns that
  relationship. Removed stage-report verdict aliases; `Verdict` now
  owns `stage_report_verdict`. Removed the unused engine-selection
  role fallback helper/table. Removed task close outcome/label tables;
  `OutcomeReasonCode` now owns `is_task_close_outcome` and
  `task_close_label`. Removed the lifecycle primary-stage table;
  `PipelineState.primary_stage` now owns that projection. The remaining
  `dict[PipelineState, ...]` results are registries/config groupings or
  persisted keyed state (`nodes`, `hook_specs`, `stage_retry`,
  `last_rejection_by_stage`), not domain-concept mappings.
- [x] M4. Investigate `latest_report_file_changes` and
  `source_agent_id=None`. Source agent should usually be present; if
  a system/operator source exists, model it explicitly.
  Source: note 3, 10:57-11:03.
- Verified 2026-05-06: `_latest_report_files_changed` in
  `litehive/agents/manager.py` now requires `source_subagent_id: str`
  and both finish/progress report paths pass `ref.id`. Added a
  regression test proving a newer activity entry from `SA-0099` cannot
  overwrite the current subagent's `files_changed` in the session
  report. Broader optional source modeling remains covered by `M5` and
  `M6`.
- [x] M5. Audit `role=None`, `task=None`, `stage=None`,
  `source_agent_id=None`, `verdict=None`, and similar optionals.
  Remove optionality where it is not a real domain state.
  Source: note 3, 11:03-11:53.
- Progress 2026-05-06: audited the post-turn verdict reader
  `latest_verdict_after` in `litehive/lifecycle/heru_factory.py`.
  `source_subagent_id` is not a real optional there because the
  production callers have either `result.ref.id` or the synthetic
  `direct-recovery` id. The parameter is now required, and lifecycle
  tests explicitly attach a source id to each queried verdict entry.
  The generic latest-activity filters remain optional
  because unfiltered activity queries are still real query shapes.
- Progress 2026-05-06: added `TaskActivityEntry.source` with explicit
  `agent`, `operator`, and `system` values. Agent-report entries set
  `source="agent"`, operator report/engine-switch entries set
  `source="operator"`, and hook/recovery-system entries set
  `source="system"`. `source_subagent_id` remains optional on the
  model for now because operator/system entries legitimately have no
  subagent id; enforcing agent-source ids is still pending.
- Verified 2026-05-07: enforced the source/subagent invariant on
  `TaskActivityEntry`: `source="agent"` now requires
  `source_subagent_id`, while `operator` and `system` entries may omit
  it. Legacy rows without `source` still load: rows with
  `source_subagent_id` infer `agent`, and rows without it infer
  `operator`. Re-audited the remaining named optionals: generic
  `TaskActivityLog.latest_entry` filters are real query shapes;
  `target_stage`, `verdict_classification`, recovery origins, audit
  before-task snapshots, and resume-stage fallbacks are real absence
  states rather than constructor shortcuts.
- [x] M6. Prefer passing a `SubAgentId` or a `SubAgent` object over a
  vague `source_agent_id`.
  Source: note 3, 11:53-12:05.
  Verified 2026-05-07: added `SubagentId = NewType("SubagentId", str)`
  in `litehive/domain/agent.py` and threaded it through the exact
  source-report path: `AgentReportIdentity.subagent_id`,
  `TaskActivityEntry.source_subagent_id`,
  `TaskActivityLog.latest_entry(...)`, `_latest_report_files_changed(...)`,
  `MissingVerdictError`, and `latest_verdict_after(...)`. Runtime
  boundaries still convert to plain strings for environment variables
  and SQLite session artifacts. Checked with ruff plus focused activity,
  CLI-report, subagent-manager, and lifecycle verdict-filter tests.
- [x] M7. Consider making latest task activity available from the
  `SubAgent`, `Task`, or `Workspace` domain object instead of a loose
  helper.
  Source: note 3, 12:05-12:13; note 4, 00:39-00:55.
  Verified 2026-05-07: added `Workspace.task_activity(task)` returning
  a `TaskActivityLog` collaborator in `litehive/tasks/activity.py`,
  moved the latest-entry query to `TaskActivityLog.latest_entry(...)`,
  removed the loose `latest_task_activity_entry(...)` helper, and
  updated the exact production readers in `litehive/agents/manager.py`,
  `litehive/agents/parsing.py`, `litehive/lifecycle/heru_factory.py`,
  and `litehive/lifecycle/runtime_sync.py`. Added
  `test_workspace_task_activity_returns_latest_matching_entry` and
  verified with ruff plus focused task-activity, stage-report,
  subagent-manager, lifecycle-verdict, and recovery-follow-up tests.
- [x] M8. Shift the codebase away from procedural piles of functions
  and toward domain objects whose responsibilities make it obvious
  who does what.
  Source: note 3, 12:13-12:36; note 4, 00:57-01:21.
  Verified 2026-05-07: completed the task-activity ownership slice
  started in M7. `TaskActivityLog` now owns `load()`, `save()`,
  `append()`, and `latest_entry()`; the public free helpers
  `load_task_activity(...)`, `save_task_activity(...)`,
  `append_task_activity(...)`, and `latest_task_activity_entry(...)`
  were removed. Updated production readers/writers in CLI report
  commands, hook reports, recovery reports/evidence, prompt
  serialization, requeue pass retraction, debug evidence, worktree
  inspection, and lifecycle verdict rewriting to use
  `workspace.task_activity(task)`. Verified no code/test references to
  the old free helpers remain, and checked the exact activity
  persistence/report/recovery/hook/prompt paths with focused tests.
- [x] M9. For errors such as `SubAgentStartupError` and
  `UnexpectedFailureBeforeTheEngineSubprocessStarted`, document which
  actor/service can throw them and under what condition.
  Source: note 3, 12:48-13:04.
  Verified 2026-05-07: documented the actual implemented exception,
  `SubagentStartupError`, at its owner in `litehive/agents/manager.py`.
  `SubagentManager.run` is the only production raiser, and it raises
  only before an engine pid/live progress confirms the subprocess
  started. Documented the lifecycle handler in
  `litehive/lifecycle/heru_factory.py`: `HeruEngineAdapter.run_turn`
  catches manager construction/run startup failures and routes them
  through `_handle_startup_failure` and, during recovery,
  `_attempt_direct_recovery_handoff`. Added the same actor/error
  ownership summary to `docs/domain.md`, including that
  `UnexpectedFailureBeforeTheEngineSubprocessStarted` is not a
  Litehive exception class; it is the descriptive condition modeled by
  `SubagentStartupError`. Verified with focused ruff and startup
  failure/direct-recovery tests.
- [x] M10. Remove `SessionMixin` from `SubAgentManager`; no mixins
  should remain.
  Source: note 3, 13:03-13:59; note 4, 10:48-11:13.
  Verified 2026-05-07: renamed the old `SessionMixin` to the concrete
  `SubagentSessionManager` collaborator in `litehive/agents/session.py`.
  `SubagentManager` no longer inherits from the session code; it owns a
  `self.sessions` collaborator and all session I/O call sites now go
  through that object. Updated direct session tests and manager tests
  to exercise `SubagentSessionManager` / `manager.sessions`. Verified
  no code/test references to `SessionMixin` remain outside checklist
  text, and checked with focused ruff plus continuation, event-stream,
  and subagent-manager tests.
- [x] M11. Replace mixins with delegated collaborators such as a
  `SessionManager` dependency.
  Source: note 3, 13:59-14:08; note 4, 10:58-11:13.
  Verified 2026-05-07: made `SubagentSessionManager` an explicit
  `SubagentManager.__init__` dependency instead of constructing it
  inside the manager. The production assembly point
  `build_subagent_manager_for_workspace` in `litehive/container.py`
  now builds the session collaborator and passes it as `sessions=...`.
  Added `test_subagent_manager_receives_session_manager_from_container`
  to lock the wiring, and reran the continuation, event-stream, and
  subagent-manager tests through the delegated `manager.sessions` path.
- [x] M12. Ensure `SubAgentManager.__init__` does not create
  `Workspace`, config, sandbox launcher, or other collaborators.
  Those dependencies must be injected.
  Source: note 3, 14:24-14:43.
  Verified 2026-05-07: audited `SubagentManager.__init__` in
  `litehive/agents/manager.py`; it now only resolves the two path
  identities and stores injected `workspace`, `config`, `sandbox`, and
  `sessions` collaborators. Workspace creation, config loading,
  `SandboxLauncher`, and `SubagentSessionManager` construction all live
  in `litehive/container.py` assembly helpers. Added
  `test_subagent_manager_constructor_stores_injected_collaborators` to
  prove direct construction uses caller-provided objects, plus the M11
  container wiring test. Verified with focused ruff and constructor /
  container tests.
- [x] M13. Review `MergeWarning`; if it is unnecessary, delete it. If
  it is necessary, explain why.
  Source: note 3, 14:54-15:07.
  Verified 2026-05-07: searched production code and tests; there is no
  `MergeWarning` implementation or caller left to justify. The
  merge-related state now lives in structured task/report/activity
  surfaces rather than a Python warning category. Added
  `test_merge_warning_type_is_not_reintroduced` in
  `tests/test_architecture_guardrails.py` so a `MergeWarning` class or
  function cannot quietly return. Verified with focused ruff and the
  new guardrail test.
- [x] M14. Replace `record_lifecycle_callback_failure` mutating a
  warnings list with a `WarningsRepository` or `WarningsService`
  that encapsulates warning storage and retrieval.
  Source: note 3, 15:07-16:07.
  Verified 2026-05-07: replaced the raw `callback_warnings: list[str]`
  mutation path in `litehive/agents/manager.py` with a
  `CallbackWarnings` collector. Live `on_started` / `on_update`
  callback failures now call `record_failure(...)`, and
  `_write_session_finish` merges report warnings through
  `CallbackWarnings.merged_with(...)`. Added
  `test_callback_warnings_merge_dedupes_collected_failures` and reran
  the start/progress callback failure tests plus the full subagent
  manager test file.
- [x] M15. Review `agent_stage_for_task`; decide whether stage
  selection belongs somewhere other than subagent code.
  Source: note 3, 16:15-17:24.
- Verified 2026-05-06: `agent_stage_for_task` moved to
  `litehive/domain/roles.py`, where it returns only reportable domain
  stage values accepted by `StageReport`.
- [x] M16. Simplify the long/nested
  `task.runtime.pipeline.current_stage.stage` style access. Current
  pipeline/stage behavior should have a clearer method such as a
  `run` function that already knows the current stage.
  Source: note 3, 16:20-17:00; note 3, 18:09-18:20.
  Verified 2026-05-07: added read-only stage accessors
  `TaskRecord.current_pipeline_stage`,
  `TaskRuntime.current_stage_name`, and
  `PipelineRuntime.current_stage_name`. Updated production read paths
  in role stage selection, task creation provenance, status rendering,
  recovery evidence, stop/resume helpers, agent reporting, and launch
  state to use the accessor instead of the nested runtime shape.
  Storage JSON queries and direct mutation tests still reference
  `runtime.pipeline.current_stage.stage` because they operate at the
  persistence/update boundary. Added
  `test_task_runtime_stage_accessors_hide_nested_current_stage_shape`
  and reran focused ruff plus the affected agent-role, status,
  workspace-health, and subagent stage tests.
- [x] M17. Review `stage_report_for_task`; it looks useless or
  under-documented. Delete it or move the real behavior to a better
  domain/service owner.
  Source: note 3, 17:29-18:06.
- Verified 2026-05-06: deleted the manager-only
  `_report_stage_for_task`; the domain selector now returns a
  `ReportPipelineState` directly, so no second narrowing helper is
  needed.
- [x] M18. Replace `get_engine` free/global access with injected
  engine management such as an `EngineManager`.
  Source: note 3, 18:20-18:52.
  Verified 2026-05-07: added `litehive/agents/engine_manager.py`
  with an `EngineManager` collaborator that owns heru engine lookup
  and resume-safe model override handling. `SubagentManager` now takes
  `engines: EngineManager` in its constructor and uses
  `self.engines.engine_for(...)` for both launch and live-progress
  observation paths; it no longer imports or calls `get_engine`
  directly. `build_subagent_manager_for_workspace` wires the
  collaborator in the container, and manager tests now patch the
  engine-manager boundary instead of `litehive.agents.manager`.
  Verified with `rg` over the exact manager/container paths, focused
  ruff, and the engine-manager, subagent-manager, subagent-event,
  stage-report-feedback, and lifecycle engine-adapter test files.
- [x] M19. Remove duplicate variables such as `execution_engine` and
  `agent` if they represent the same concept.
  Source: note 3, 18:52-19:05.
  Verified 2026-05-07: reviewed the exact `SubagentManager.run`
  launch path. The old `engine` / `execution_engine` pair mixed two
  concepts: the underlying heru adapter used for availability and
  capability checks, and the adapter that actually runs the process
  after optional sandbox wrapping. Renamed these to
  `engine_adapter` and `run_adapter`, removed the redundant
  `live_execution_probe` and `callback_probe` aliases, and kept
  capability checks on the underlying adapter while execution and
  final engine-monitoring recording use `run_adapter`. Verified with
  `rg` that `execution_engine`, `live_execution_probe`, and
  `callback_probe` are gone from `litehive/agents/manager.py`, then
  reran focused ruff and the full subagent-manager test file.
- [x] M20. Clarify or rename `SubagentRef`; if it is just a subagent,
  name/model it as a subagent.
  Source: note 3, 19:11-19:20; note 6, 00:09-00:21.
  Verified 2026-05-07: traced `SubagentRef` to heru's transport
  model and Litehive's re-export in `litehive/domain/runtime.py`.
  Litehive stores that shape as the actual persisted subagent record
  on `TaskRecord.subagents`, so production code now uses the
  Litehive-owned `Subagent` name. `SubagentRef` remains only as a
  compatibility alias in `domain.runtime` for older tests and
  construction helpers. Updated manager/session/runtime/task/report
  annotations and docs to use `Subagent`, and added
  `test_production_code_uses_subagent_name_not_subagent_ref` so the
  old name cannot spread back into production code. Verified with
  `rg`, focused ruff, and affected runtime, manager, event-stream,
  execution-trace, lifecycle-adapter, prompt-serializer, and
  architecture guardrail tests.
- [x] M21. Replace hard-coded status strings such as `"running"` with
  enums.
  Source: note 3, 19:20-19:27.
  Verified 2026-05-07: added a Litehive-owned
  `SubagentStatus` string enum in `litehive/domain/common.py` with
  the heru-compatible persisted values. Updated the exact subagent
  status paths in `SubagentManager.run`, session snapshot handling,
  execution-trace loading, interrupted-subagent recovery, recovery
  evidence, and recovery prompt diagnostics to use `SubagentStatus`
  instead of raw `"running"` / `"completed"` / `"failed"` /
  `"blocked"` / `"interrupted"` literals. Kept `.value` at the heru
  `Subagent` assignment boundary where that model stores literal
  strings. Added
  `test_subagent_status_is_domain_enum_with_heru_serialized_values`
  and verified with `rg` that those exact subagent paths no longer
  contain raw status-string assignments/comparisons, plus focused ruff
  and affected manager, event-stream, execution-trace, recovery, and
  domain tests.
- [x] M22. Define the role of `SubAgentManager` in domain docs. Its
  responsibility is currently unclear and appears too broad.
  Source: note 3, 19:35-20:09.
  Verified 2026-05-07: inspected the exact
  `litehive/agents/manager.py` run path and its collaborators, then
  added `docs/domain.md` "Subagent Execution Boundary". The doc now
  defines `SubagentManager` as the per-invocation coordinator for one
  external subagent process and lists the sequence it owns: id/artifact
  allocation, `Subagent` attachment, engine resolution, sandbox
  wrapping, callback wiring, exit/startup classification, final report
  snapshot, and engine monitoring. It also names the responsibilities
  that belong elsewhere: lifecycle routing, prompt policy, engine
  registry lookup, session/artifact I/O, sandbox policy, activity
  storage, and report parsing. Added
  `test_domain_docs_define_subagent_manager_boundary` so the boundary
  remains documented. Verified with focused ruff, the new guardrail
  test, and `rg` across the manager/session/engine/sandbox/parsing
  paths referenced by the doc.
- [x] M23. Review `save_on_update`, engine update callbacks, and
  callback handling. Move callback responsibilities to clearer
  collaborators if they do not belong in the manager.
  Source: note 3, 20:16-20:38.
  Verified 2026-05-07: traced the live callback path in
  `SubagentManager.run`: `on_started` records PID metadata and
  `on_update` writes live progress snapshots while converting
  callback persistence failures into final report warnings. Extracted
  that best-effort callback behavior from nested manager functions
  into `litehive/agents/callbacks.py` as `SubagentRunCallbacks` and
  moved `CallbackWarnings` there. The manager now only constructs the
  callback collaborator, passes `callbacks.on_started` /
  `callbacks.on_update` into heru, checks `callbacks.engine_started`
  at the startup-failure boundary, and merges `callbacks.warnings` at
  finish. Updated `docs/domain.md` to name callback best-effort
  handling as owned by `SubagentRunCallbacks`, and extended the domain
  doc guardrail. Added `tests/agents/test_callbacks.py`; verified
  with `rg` that the old nested `_safe_on...` callback functions and
  `nonlocal engine_started` are gone, plus focused ruff, callback
  tests, full subagent-manager tests, and the domain-doc guardrail.
- [x] M24. Remove unnecessary casts such as in `run_live_callable`;
  if the cast remains, fix the typing issue that required it.
  Source: note 3, 20:38-20:48.
  Verified 2026-05-07: traced the casts in
  `litehive/agents/manager.py` and `litehive/agents/sandbox_support.py`
  to `heru.engine_detection.effective_engine_callable(...)` returning
  `object | None`. Added `litehive/agents/engine_callables.py` with
  `resolve_cli_execution_callable(...)`, which performs the callable
  check once and returns `Callable[..., CLIExecutionResult]`.
  Replaced the manager and sandbox `run_callable` /
  `run_live_callable` casts with the helper and removed the now-unused
  `cast` imports. Added `tests/agents/test_engine_callables.py` for
  successful resolution and missing-method failure. Verified with `rg`
  that the manager/sandbox call paths no longer contain `cast(...)`,
  plus focused ruff, engine-callable tests, full subagent-manager
  tests, and sandbox integration tests.
- [x] M25. Break the manager `run` method into clearly named smaller
  methods.
  Source: note 3, 20:48-21:24.
  Verified 2026-05-07: traced `SubagentManager.run` in
  `litehive/agents/manager.py` and split it into named phases:
  `_prepare_subagent_run`, `_execute_subagent_engine`,
  `_run_engine_process`, `_run_live_engine_process`,
  `_run_single_engine_process`, `_classify_completed_execution`, and
  `_finalize_subagent_run`. Added `SubagentRunContext`,
  `EngineProcessResult`, and `EngineRunOutcome` so the helpers pass
  named run state instead of positional tuples. Verified with `rg` that
  `run` now only orchestrates those phases, fixed the completed
  inactivity-timeout branch to preserve the selected run adapter, and
  reran focused ruff plus full subagent-manager tests.
- [x] M26. Replace `next_subagent_id` based on on-disk folders with a
  database-backed ID repository or domain service.
  Source: note 3, 21:24-22:24.
  Verified 2026-05-07: traced the old allocation path in
  `SubagentManager._prepare_subagent_run` and removed the
  `_next_subagent_id` helper that scanned `task_dir(...)/subagents`.
  Added `litehive/agents/subagent_ids.py` with
  `SubagentIdRepository.reserve_next_id(...)`, backed by the new
  SQLite `subagent_id_counters` table from migration
  `0010_subagent_id_counters.sql`. The repository advances the counter
  in one DB transaction and seeds only from persisted task refs plus
  `subagent_sessions` rows for upgraded workspaces; artifact folder
  names are no longer consulted. Wired the repository through
  `build_subagent_manager_for_workspace`, added repository tests, and
  added a manager test proving a stale `SA-0099-*` artifact directory
  does not affect the next allocated id. Verified with `rg`, focused
  ruff, full subagent-manager tests, subagent-id tests, and migration /
  workspace bootstrap checks.
- [x] M27. Remove `execution is None` handling when execution should
  always exist. If it is absent, fail loudly instead of swallowing the
  broken state.
  Source: note 3, 22:35-23:38.
  Verified 2026-05-07: traced the real manager execution path from
  `_execute_subagent_engine` through `_finalize_subagent_run`,
  `_write_session_finish`, `_parse_execution_report`, and
  `SubagentSessionManager.render_execution_trace` /
  `extract_execution_continuation`. Removed the started
  `EngineError`/`SandboxError` branch that created `proc = None`,
  `exit_code = 0`, empty stdout/stderr, and a synthetic blocked
  result. Post-start engine errors now re-raise, while pre-start
  errors still become `SubagentStartupError`. Tightened
  `EngineRunOutcome.execution`, finish snapshot writing, report
  parsing, and session trace/continuation helpers to require a real
  `CLIExecutionResult`. Added a manager regression test proving a
  post-start `EngineError` is propagated instead of fabricating an
  execution. Verified with `rg` that manager/session code no longer
  contains `execution is None`, `proc is None`, `proc = None`, or
  optional execution annotations, then reran focused ruff and
  subagent-manager/session/report tests.
- [x] M28. Replace report payload dictionaries with typed classes.
  Source: note 3, 23:53-24:08.
  Verified 2026-05-07: traced the report snapshot path through
  `SubagentManager._write_session_finish`,
  `SubagentManager.write_session_progress`,
  `SubagentSessionManager.write_session_start`, and
  `SubagentSessionManager.write_session_snapshot`. Added
  `litehive/agents/session_reports.py` with typed
  `SubagentReportPayload`, changed manager/start/progress/final
  snapshot construction to instantiate that payload instead of raw
  dictionaries, and moved JSON-compatible serialization to
  `SubagentReportPayload.as_dict()` inside the storage boundary.
  Existing `load_subagent_report(...)` readers still receive the same
  dict shape from SQLite. Added `tests/agents/test_session_reports.py`
  for serialization copies and verified with `rg` that the
  manager/session snapshot path no longer has `report_payload = {...}`,
  `report_payload: dict`, or `report=report_payload`. Reran focused
  ruff, subagent report/session tests, recovery tests, and recovery
  prompt diagnostic serialization.
- [x] M29. Review `write_session_snapshot` and similar functions with
  many parameters. Reduce parameters by passing meaningful domain
  objects.
  Source: note 3, 24:08-24:47.
  Verified 2026-05-07: traced all `write_session_snapshot(...)` and
  `write_session_metadata(...)` callers in `SubagentManager` and
  `SubagentSessionManager`. Added
  `litehive/agents/session_snapshots.py` with
  `SubagentSessionMetadata` and `SubagentSessionSnapshot`, changed
  full snapshot writes to pass one `snapshot=` object instead of the
  parallel `prompt`, `transcript`, `stdout`, `stderr`,
  `report_payload`, `exit_code`, `pid`, `interruption_reason`, and
  `continuation` fields, and changed metadata-only writes to pass a
  typed metadata object. Added `tests/agents/test_session_snapshots.py`
  for the new snapshot/metadata types. Verified with `rg` that the
  old snapshot call shape is gone from `litehive/agents`, then reran
  focused ruff, subagent snapshot/report/manager/event-stream tests,
  and recovery prompt-reader tests.
- [x] M30. Replace `append_event(..., data=dict)` with a typed event
  object and consider `Workspace.append_event(task, event)`.
  Source: note 3, 25:10-25:49.
  Verified 2026-05-07: traced the durable subagent event path through
  `SubagentSessionManager.write_session_start`,
  `SubagentSessionManager.record_subagent_pid`,
  `SubagentManager.write_session_progress`, and
  `SubagentManager._write_session_finish`. Added typed subagent event
  objects in `litehive/agents/session_events.py`
  (`SubagentStartedEvent`, `SubagentPidEvent`,
  `SubagentProgressEvent`, `SubagentFinishedEvent`) and changed
  `litehive/observability/events.append_event(...)` to accept a typed
  `PersistedTaskEvent` protocol instead of `(kind, data=dict)`.
  Added `Workspace.append_event(task, event)` and moved subagent
  callers to that workspace-owned API. Added
  `tests/agents/test_session_events.py` for event serialization and
  persistence. Verified with `rg` that subagent code no longer imports
  `append_event` directly or calls it with `data={...}`, then reran
  focused ruff, subagent manager/event-stream/event tests, and recovery
  prompt-reader tests.
- [x] M31. `SubAgent.Finished` / finished status should be an enum,
  not a raw string.
  Source: note 3, 25:39-25:49.
  Verified 2026-05-07: traced the subagent status path through
  `SubagentManager._classify_completed_execution`,
  `SubagentReportPayload`, and `SubagentFinishedEvent`. The Heru
  `SubagentRef` transport field still serializes to Heru's string
  literal status vocabulary, but Litehive-owned report/event payloads
  now require `SubagentStatus` and serialize to `.value` only at the
  SQLite/JSON boundary. Kept Heru ref status assignments as string
  `.value` writes for type compatibility, and convert the Heru ref
  status into `SubagentStatus(...)` when constructing typed report
  and finished-event objects. Updated session report/event tests to
  construct enum statuses and verified with `rg` that Litehive-owned
  subagent report/finished-event paths no longer accept raw
  `"completed"` / `"failed"` / `"running"` status strings. Reran
  focused typecheck, subagent session/event/manager tests, and full
  project typecheck/tests.
- [x] M32. Split `write_session_metadata` into success/error flows so
  `exit_code` and `interruption_reason` are not vague nullable
  fields.
  Source: note 3, 26:52-27:20.
  Verified 2026-05-07: replaced the broad metadata-only
  `write_session_metadata(...)` path with
  `write_running_session_metadata(...)`, backed by
  `RunningSubagentSessionMetadata`. Traced both metadata-only call
  sites: PID recording in `SubagentSessionManager.record_subagent_pid`
  and live progress writes in `SubagentManager.write_session_progress`
  now pass only running fields (`pid`, optional continuation). Terminal
  `exit_code` and `interruption_reason` remain on full
  `SubagentSessionSnapshot` writes, where completion/error handling has
  the process result. Verified with `rg` that the old writer name is no
  longer present in code, and reran focused ruff plus subagent session,
  manager, event-stream, callback, and recovery tests.
- [x] M33. Replace session dictionaries with concrete session objects.
  PID and exit code should not be `None` where the process model says
  they must exist.
  Source: note 3, 27:20-27:47.
  Verified 2026-05-07: added concrete persisted session row objects:
  `RunningSubagentSessionRow` for start/progress snapshots and
  `TerminalSubagentSessionRow` for finished snapshots. Added
  `InterruptedSubagentSessionRow` for resumable interrupted subagents
  where an exit code may not exist yet. The main
  `SubagentSessionManager.write_running_session_metadata` and
  `SubagentSessionManager.write_session_snapshot` paths now pass
  `session=session_row` to the persistence boundary, while
  `save_subagent_artifacts` owns final `as_dict()` serialization.
  Converted direct recovery bypass in
  `HeruEngineFactory._run_direct_recovery_turn` from a raw
  `session={...}` dict to `RunningSubagentSessionRow`, and converted
  interrupted-session persistence in
  `_write_interrupted_subagent_artifacts` from mutating a loaded
  session dictionary to `InterruptedSubagentSessionRow`. Kept nullable
  PID only on running/progress/interrupted rows because Heru progress
  callbacks can arrive before process-start PID reporting, and
  interrupted processes may not have a terminal exit code. Verified with
  `rg` that production `save_subagent_artifacts(..., session=...)`
  paths now pass row objects rather than raw dictionaries, and reran
  focused ruff, pyrefly, subagent manager/session/event-stream tests,
  recovery tests, and direct-recovery lifecycle tests.
- [x] M34. Every saved event needs a concrete domain role and a
  description of why it is persisted and where it is used.
  Source: note 3, 27:53-28:24.
  Verified 2026-05-07: `SubagentPidEvent` and
  `SubagentProgressEvent` now include the concrete subagent `role` in
  their persisted data, matching `SubagentStartedEvent` and
  `SubagentFinishedEvent`. Added `persistence_reason` and `consumed_by`
  metadata to each typed subagent event class so the reason for
  persistence and the consumer surfaces are visible next to the event
  contract. Traced all `Workspace.append_event(...)` subagent call
  sites: start/PID writes in `SubagentSessionManager`, progress/finish
  writes in `SubagentManager`. Verified with event serialization tests,
  focused subagent manager/event-stream tests, pyrefly, ruff, and
  operator status/activity tests.
- [x] M35. Replace `resource_control_as_dict` and
  `sandbox_policy_summary` dictionary conversion with typed objects,
  or document why serialization requires a dict.
  Source: note 3, 28:30-28:55.
  Verified 2026-05-07: `SubagentReportPayload` and
  `SubagentSessionStorageFields` now carry `SandboxPolicySummary`
  directly and serialize through `.as_dict()` only at the persisted
  JSON boundary. Added `SandboxPolicySummary.from_mapping(...)` for
  rehydrating historical session payloads during interrupted-subagent
  recovery. Traced `SubagentSessionManager.session_storage_fields`,
  `SubagentManager._write_session_finish`,
  `SubagentManager.write_session_progress`,
  `_write_interrupted_subagent_artifacts`, and
  `HeruEngineFactory._run_direct_recovery_turn`: they now pass typed
  `SandboxPolicySummary` objects rather than resource-control dicts.
  Verified with `rg` that `policy_summary(...).as_dict()` is gone from
  the manager/session construction path and only report/session
  serializers call `.as_dict()`. Reran focused ruff, pyrefly, sandbox
  integration tests, subagent session/report/manager tests, recovery
  tests, and direct-recovery lifecycle tests.
- [x] M36. Move `load_subagent_session` toward
  `Workspace.load_subagent_session(...)`, `Task.load_subagent_session`
  or another object-owned API.
  Source: note 3, 28:58-29:23.
  Verified 2026-05-07: added
  `Workspace.load_subagent_session(task_id, subagent_id)` as the
  object-owned API and migrated production callers in
  `SubagentSessionManager`, interrupted-subagent recovery,
  `litehive agent report`, task logs/debug support, and recovery-role
  diagnostics. Kept `agents.session_store.load_subagent_session(...)`
  as the storage implementation behind the workspace method. Verified
  with `rg` that all non-storage call sites now go through
  `workspace.load_subagent_session(...)`, then reran focused ruff,
  pyrefly, subagent manager/recovery tests, agent-report/log/debug
  CLI tests, prompt serializer tests, and operator status/activity
  tests.
- [x] M37. Collapse the chain from existing session load to
  `load_subagent_artifacts` into a simpler typed call if possible.
  Source: note 3, 29:29-29:48.
  Verified 2026-05-07: added `SubagentArtifactSlice` and
  `_load_subagent_artifact_slice(...)` in `agents.session_store`.
  `load_subagent_session`, `load_subagent_report`, and
  `load_subagent_event_stream` now select a typed payload slice
  directly instead of calling the public full-payload
  `load_subagent_artifacts(...)` helper and indexing by raw string.
  Kept `load_subagent_artifacts(...)` for consumers that need the full
  bundle, such as recovery evidence. Verified with `rg` that only full
  bundle consumers call `load_subagent_artifacts(...)`, then reran
  focused ruff, pyrefly, session-store tests, subagent manager tests,
  event-stream tests, and recovery tests.
- [x] M38. Remove `isinstance(existing.get("created_at"), ...)` by
  making session loading return a typed object, not a dictionary.
  Source: note 3, 29:55-30:15.
  Progress 2026-05-07: added
  `Workspace.load_subagent_session_created_at(...) -> str | None` and
  moved the creation-time type narrowing behind the workspace API.
  `SubagentSessionManager` no longer performs
  `isinstance(existing.get("created_at"), str)` in its write paths.
  Progress 2026-05-07: added a typed `LoadedSubagentSession` storage
  record and routed `Workspace.load_subagent_session_created_at(...)`
  through it, so creation-time normalization now happens at the
  persistence boundary while `Workspace.load_subagent_session(...)`
  remains dictionary-compatible for current readers.
  Progress 2026-05-07: exposed
  `Workspace.load_subagent_session_record(...)` as the typed
  object-owned API and moved interrupted-subagent persistence to use
  the typed `created_at` field instead of reading it from the session
  dictionary.
  Progress 2026-05-07: moved `litehive agent report` identity
  resolution to `Workspace.load_subagent_session_record(...)` and
  typed `LoadedSubagentSession.subagent_id` / `.role` accessors,
  removing raw dictionary `isinstance` checks from that path.
  Progress 2026-05-07: added
  `LoadedSubagentSession.exit_code` and moved task debug's
  subagent-line exit-code reader to the typed session API.
  Progress 2026-05-07: added `LoadedSubagentSession.updated_at`
  and moved the latest-subagent debug summary to typed session fields.
  Progress 2026-05-07: moved task-log subagent listing to
  `Workspace.load_subagent_session_record(...)` for persisted
  exit-code and duration fields, leaving runtime-state precedence in
  a runtime-only helper.
  Verified 2026-05-07: moved the last production
  `Workspace.load_subagent_session(...)` reader, recovery prompt
  assembly, to `Workspace.load_subagent_session_record(...)` while
  preserving the dictionary payload in the prompt context. `rg` now
  shows `load_subagent_session(...)` only at the compatibility method
  and storage implementation; production callers use the typed record.
- [x] M39. Review continuation handling. If continuation is always
  required in a given flow, remove `None`; if not, model the distinct
  start/continue states.
  Source: note 3, 30:21-30:53; note 6, 00:47-00:59.
  Verified 2026-05-07: introduced explicit subagent continuation
  state objects (`NoSubagentContinuation` and
  `CapturedSubagentContinuation`) for session/report persistence.
  Subagent report payloads, session snapshot metadata, concrete
  session rows, running progress snapshots, terminal snapshots, and
  interrupted-subagent persistence now carry a
  `SubagentContinuationState` instead of raw
  `RuntimeEngineContinuation | None` or pre-serialized dictionaries.
  Added lifecycle `FreshEngineSession` and
  `ResumableEngineSession` continuation states behind `Session`,
  plus object methods for reading and capturing the engine resume id.
  The crash-resume adapter now uses those methods instead of reading
  and writing `Session.engine_session_id` directly. Traced subagent
  artifact persistence, interrupted-subagent persistence, lifecycle
  `Session`, and `HeruEngineAdapter._run_with_crash_resume`; the only
  remaining continuation `None` handling is at the Heru boundary helper
  or legacy extraction fallback, while production report/session
  construction carries modeled start-vs-continue state. Reran focused
  ruff, pyrefly, subagent report/session/manager/event-stream/recovery
  tests, lifecycle SQLite session tests, engine-adapter tests, and
  agent retry continuation tests.
- [x] M40. Revisit `subagent_inactivity_timeout_seconds`,
  `open code inactivity timeout`, and `compiled inactivity pattern`;
  make each previous small note into a separate task.
  Source: note 3, 31:00-31:24; note 4, 10:15-10:43.
  Verified 2026-05-07: extracted
  `SubagentInactivityTimeoutPolicy` and inject it through the
  container into `SubagentSessionManager`. The policy now owns the
  opencode live-timeout exception and completed-process stderr marker
  parsing, while the manager delegates instead of hard-coding those
  rules inline. Split the remaining session-module timeout/stale-PID
  notes into separate follow-up items under SE2 and SE7 so each rule
  can be verified independently before the broader session split.
  Traced `SubagentInactivityTimeoutPolicy.live_timeout_seconds`,
  `SubagentInactivityTimeoutPolicy.completed_timeout`,
  `SubagentSessionManager.check_stdout_inactivity`, and
  `SubagentSessionManager.terminate_stale_pid`; reran focused
  inactivity-policy and subagent-manager tests.
- [x] M41. Decide why `merge_resolver` is in the current package and
  move it if its package ownership is wrong.
  Source: note 3, 31:24-31:36.
  Verified 2026-05-07: `run_worktree_merge_agent(...)` already lives
  in `litehive.agents.merge_resolver`, which matches the ownership
  rule from the older feedback: worktree code detects the conflict
  and calls into the agent package, while the agent package owns
  subagent invocation. Confirmed `litehive/worktree/execution_root.py`
  is only a caller and does not inline the merge-resolver agent.
  Replaced the local raw role string with `AgentRole.MERGE_RESOLVER`
  so the remaining role value comes from the domain role vocabulary.
- [x] M42. Rename `agents/parsing.py`; the note says this is not
  really parsing because structured output already exists. Use a name
  that reflects verdict/report extraction or repository loading.
  Source: note 3, 31:36-32:19.
  Verified 2026-05-07: renamed `litehive.agents.parsing` to
  `litehive.agents.report_extraction`. The module extracts a
  `StageReport` from the latest agent CLI activity entry and raises
  `MissingVerdictError` when no verdict was submitted; it does not
  parse raw agent text. Updated production and test imports.

## Parsing, Domain Model, And Verdict Instructions

- [x] D1. Convert verdicts such as reject/fail into domain enums.
  Source: note 4, 00:01-00:12.
  Verified 2026-05-07: `TaskActivityEntry.verdict` now uses the
  `Verdict` domain enum through `TaskActivityVerdict`, validates that
  persisted activity rows stay on the supported submitted-verdict
  vocabulary, and still rejects the removed `fail` activity alias.
  `litehive agent report` and operator `litehive report` convert CLI
  strings to `Verdict` at the boundary, the per-role allow-list stores
  enum members, and system activity writers in hook, recovery,
  hallucinated-pass retraction, and engine-switch paths pass enum
  members. Verified with focused activity, report, and verdict
  consumer tests plus ruff and pyrefly on the touched paths.
- [x] D2. Replace `FailureDiagnostics` dictionaries with a typed
  class/dataclass.
  Source: note 4, 00:14-00:20.
  Verified 2026-05-07: added `FailureDiagnostics` as the typed
  `StageReport.failure_diagnostics` and
  `TaskOutcomeState.failure_diagnostics` value object. Existing
  report/outcome constructors still accept plain dictionaries at the
  boundary and persisted JSON remains an object, but report and
  outcome consumers now read a named domain type rather than an
  anonymous dictionary.
- [x] D3. Re-read and verify every previous `agents/parsing.py`
  comment; the note says the same feedback keeps recurring.
  Source: note 4, 00:25-00:31.
  Verified 2026-05-07: re-read the older feedback and
  `docs/code-analysis-2026-05-03.md` entries for the old
  `agents/parsing.py`. Current `agents.report_extraction` has no
  `root` parameter/default, uses the domain `REPORT_VERDICT_KINDS`,
  has no `type: ignore`, treats missing agent verdicts as
  `MissingVerdictError` instead of synthetic rejects, and now hoists
  summary plus typed `FailureDiagnostics` construction into named
  helpers.
- [x] D4. Review the domain model separately across the codebase.
  The target is readability and domain ownership, not a pile of
  unclear functions.
  Source: note 4, 00:57-01:21.
  Verified 2026-05-07: audited the domain package and selected the
  dirty-worktree pool gate as the next exact domain-ownership slice.
  `DirtyWorktreeFinding.location_kind` and `ownership` now canonicalize
  to `DirtyWorktreeLocationKind` and `DirtyWorktreeOwnership`, and
  `DirtyWorktreeOwnership.blocks_pool` owns the pool-blocking decision
  instead of `DirtyWorktreeGateReport` checking a loose string set.
  `inspect_dirty_worktree_gate` now constructs those enum members, and
  focused domain, worktree, and status-rendering tests verify the
  exact producer/report/renderer path. The remaining domain-model
  concerns stay split into the concrete D5-D19 checklist items below.
- [x] D5. `lastTaskActivityEntry` or equivalent should likely be a
  method on `Workspace`, `Task`, or a task/workspace service.
  Source: note 4, 00:39-00:55.
  Verified 2026-05-07: `Workspace.task_activity(task)` returns the
  task-owned `TaskActivityLog` service, which already owned
  `latest_entry(...)`. Added `TaskActivityLog.latest()` for the
  unfiltered newest-entry case and moved task debug output off
  `load()[-1]` onto that object-owned API.
- [x] D6. In `domain/agent.py`, review `SubAgentResult` and explain
  what `subagent_ref` means.
  Source: note 6, 00:01-00:21.
  Verified 2026-05-07: reviewed `SubagentResult.ref` and documented
  that it is the persisted Litehive `Subagent` record for this run
  (the entry appended to `TaskRecord.subagents`, carrying id, role,
  engine, status, and artifact path), not a git ref or Heru transport
  reference.
- [x] D7. Change `execution_trace` from one string to a list or typed
  trace if it represents multiple agent actions.
  Source: note 6, 00:21-00:34.
  Completed 2026-05-07: added `ExecutionTrace` as the domain value
  carried by `SubagentResult.execution_trace`. It stores rendered trace
  chunks explicitly while preserving a `.text` boundary for Markdown
  artifacts and operator-facing output.
- [ ] D8. Remove unnecessary `None` values from domain agent models.
  Keep optionality for true failure-only states, such as failure
  details when not every execution fails.
  Source: note 6, 00:39-01:20.
- [ ] D9. Move utility concepts out of `domain/common.py` when they
  are not domain concepts: `utc_now`, feedback cap/truncation marker,
  `cap_feedback`, and similar helpers.
  Source: note 6, 01:36-02:10.
- [ ] D10. Split `domain/common.py` if section comments are a sign
  that too many unrelated concepts live in one file.
  Source: note 6, 02:10-02:22.
- [ ] D11. Consider renaming `OutcomeKind` to `TaskOutcomeKind`.
  Source: note 6, 02:22-02:25.
- [ ] D12. Document the relationship between outcome kind, outcome
  reason code, and verdict.
  Source: note 6, 02:33-04:33.
- [ ] D13. Structure the domain documentation with clear sections and
  lists. It currently reads like many things mixed together.
  Source: note 6, 03:45-04:15.
- [ ] D14. Explain how `Verdict.FAIL`, `Verdict.REJECT`, outcome
  kind, and outcome reason code differ.
  Source: note 6, 04:15-04:33.
- [ ] D15. Prevent unsupported verdicts from being committed or
  serialized. Add tests or validation that catches them.
  Source: note 6, 04:46-04:55.
- [ ] D16. Remove duplicate values between verdict enums and outcome
  reason codes, such as done/won't-do/defer/duplicate appearing in
  multiple places without clear distinction.
  Source: note 6, 04:55-05:06.
- [ ] D17. Decide and document whether execution cancelled and
  execution interrupted are distinct concepts.
  Source: note 6, 05:06-05:13.
- [ ] D18. For every outcome reason code, document who can set it and
  under what circumstances.
  Source: note 6, 05:13-05:34.
- [ ] D19. Explain or delete unclear reason codes such as
  `hallucinated_completion` and `blocked_on_follow_up`.
  Source: note 6, 05:34-05:51.
- [ ] D20. Generalize the domain-model critique across the whole code
  base and record durable rules in `docs/code-style.md`.
  Source: note 6, 06:02-06:17.

## Sandbox Instructions

- [ ] S1. Move `litehive/agents/sandbox_support.py` and
  `litehive/agents/sandbox.py` behavior into the sandbox module or
  package. Sandbox behavior should not live under `agents`.
  Source: note 4, 01:27-01:51.
- [ ] S2. Split `SandboxSupport`; it is too large.
  Source: note 4, 02:01-02:07.
- [ ] S3. Remove the `__all__` export bag from sandbox support.
  Source: note 4, 02:07-02:17.
- [ ] S4. Re-read previous sandbox comments and verify the exact old
  code paths, because the note says old comments still remain.
  Source: note 4, 02:17-02:49.
- [ ] S5. Fix `sandbox.policy.summary.as_dict` docstring so its
  caller and reason are clear.
  Source: note 4, 02:53-03:07.
- [ ] S6. Turn `sandbox_profile_for_all` into a method on an
  appropriate object.
  Source: note 4, 03:11-03:17.
- [ ] S7. Remove confusing assignments/aliases like
  `sandbox.adapter = litehive.sandbox.adapter`.
  Source: note 4, 03:21-03:29.
- [ ] S8. Avoid `as_dict` in sandbox policy/profile code unless a
  serialization boundary demands it. Prefer dataclasses or typed
  config objects.
  Source: note 4, 03:31-03:55.
- [ ] S9. Audio check: inspect `SandboxLauncher` around the line
  mentioned as 124 and remove the unnecessary split/delete of role or
  similar value. The note says: if it is not needed, remove it
  directly; do not split/delete it indirectly.
  Source: note 4, 04:03-04:31.
- [ ] S10. Simplify sandbox policy to global enabled/disabled config.
  Do not maintain separate per-engine sandbox policy unless a real
  need exists.
  Source: note 4, 04:39-05:25.
- [ ] S11. Move default policy handling into config loading. The
  launcher should receive an already resolved policy and should not
  contain fallback/default branches.
  Source: note 4, 05:37-06:23.
- [ ] S12. Create clear sandbox abstractions: generic sandbox launcher
  behavior and a Docker-specific implementation such as
  `DockerSandboxLauncher`.
  Source: note 4, 06:36-07:47.
- [ ] S13. Keep Docker-specific wrapping in Docker-specific code.
  Shared abstractions should only contain behavior that applies to
  all sandbox backends.
  Source: note 4, 06:38-07:47.
- [ ] S14. Remove `object` and `unset` sentinel patterns in sandbox
  artifact/session code. Use concrete dataclasses and explicit domain
  states.
  Source: note 4, 07:55-08:23.
- [ ] S15. `save_subagent_artifacts` should accept typed session,
  report, and event stream objects, not dictionaries or untyped
  objects.
  Source: note 4, 08:07-08:23.

## Subagent Artifact Ownership Instructions

- [ ] SA1. Model a subagent as belonging to a workspace and a
  concrete task.
  Source: note 4, 08:47-08:59.
- [ ] SA2. Give the subagent/session object enough data to save its
  own artifacts: session, report, and event stream.
  Source: note 4, 08:59-09:09.
- [ ] SA3. Replace scattered `save_subagent_artifacts` free-function
  calls with a method such as `subagent.save_artifacts(...)` or a
  session-store service method invoked by the subagent object.
  Source: note 4, 08:59-09:19.
- [ ] SA4. Define these as domain rules so artifact/session behavior
  is not a pile of functions spread through the code.
  Source: note 4, 09:19-09:29.
- [ ] SA5. Audit every function/method whose first argument is
  `workspace`. Most should become a method on `Workspace` or a
  focused workspace service.
  Source: note 4, 09:33-09:49.

## Session Module Instructions

- [ ] SE1. Review and likely split the session module; it is about
  400 lines and too large.
  Source: note 4, 10:01-10:12.
- [ ] SE2. Revisit `open code inactivity timeout` and
  `compiled inactivity pattern`; the same notes were already given
  before, so extract each small item into its own task.
  Source: note 4, 10:15-10:43.
- [ ] SE2a. Decide whether the opencode 300s live stdout timeout
  exception should remain hard-coded policy, become engine config, or
  move to engine capability metadata.
  Source: note 4, 10:15-10:43; extracted from M40.
- [ ] SE2b. Decide whether completed-process inactivity detection
  should keep parsing Heru's stderr marker or receive a typed timeout
  result from the engine adapter.
  Source: note 4, 10:15-10:43; extracted from M40.
- [ ] SE2c. Verify live adapter timeout propagation for every engine
  path, including engines without live execution support.
  Source: note 4, 10:15-10:43; extracted from M40.
- [ ] SE3. Remove `SessionMixin` and use a delegated session manager
  dependency.
  Source: note 4, 10:48-11:13.
- [ ] SE4. Review `session.render_execution_trace`; it may not be
  session's responsibility.
  Source: note 4, 11:20-11:27.
- [ ] SE5. Find all `del ...` patterns such as deleting
  `engine_name`. If a value is immediately deleted, stop passing it.
  Source: note 4, 11:30-11:43.
- [ ] SE6. Consolidate `append_stream_data` and related event tracking
  into one owner instead of spreading it through code.
  Source: note 4, 11:56-12:17.
- [ ] SE7. Review `terminate_stale_pid` and inactivity behavior while
  splitting session responsibilities.
  Source: note 4, 12:27-12:31.
- [ ] SE7a. Decide whether live stdout inactivity detection belongs in
  the session manager, the engine-process runner, or a dedicated
  watchdog collaborator.
  Source: note 4, 12:27-12:31; extracted from M40.
- [ ] SE7b. Decide whether `terminate_stale_pid` should stay as a
  best-effort session helper or move to the shared process-signal
  owner used by task close/stop flows.
  Source: note 4, 12:27-12:31; extracted from M40.
- [ ] SE8. Split `write_session_snapshot`; it is too large.
  Source: note 4, 12:47-12:53.

## CLI And Command Instructions

- [ ] C1. Remove domain strings from `agent_cli` / agency alignment
  code. Values submitted by agents must be domain enums, not raw
  strings hard-coded in CLI.
  Source: note 4, 12:53-14:07.
- [ ] C2. Keep CLI thin: parse user input, create/load the right
  domain object, and dispatch. Do not keep business logic in CLI.
  Source: note 4, 14:00-14:07; note 4, 16:57-19:09.
- [ ] C3. Convert `Workspace` from a dataclass to a normal class.
  Source: note 4, 14:33-15:12.
- [ ] C4. In resolve reported entity flows, pass a workspace/session
  object that loads the session. `load_subagent_session` must return a
  typed object, not a dictionary.
  Source: note 4, 15:29-16:14.
- [ ] C5. Remove `isinstance` checks from session/report resolution
  by returning typed objects.
  Source: note 4, 15:57-16:14.
- [ ] C6. Move `resolve_active_agent_task_mutation_target` style
  business logic out of CLI.
  Source: note 4, 16:57-18:37.
- [ ] C7. Replace scattered `os.environ.get("LITEHIVE_TASK_ID")` and
  `os.environ.get("LITEHIVE_WORKSPACE_ROOT")` with a container/config
  boundary that reads env once and passes parameters.
  Source: note 4, 16:57-18:06.
- [ ] C8. Audit all CLI modules yourself for any remaining business
  logic and move it into domain/container services.
  Source: note 4, 19:15-19:28.
- [ ] C9. In `cli/engine.py`, inspect the line where config is loaded
  and then apparently unused. Return or use the config correctly, or
  remove the unnecessary load.
  Source: note 4, 19:28-20:00.
- [ ] C10. Split long engine command functions according to the
  25-line function-size rule.
  Source: note 4, 20:00-20:36.
- [ ] C11. Rework `EngineCommand`: remove default/preference command
  behavior from CLI if it can be configured in YAML; keep engine
  freeze and on-freeze behavior.
  Source: note 4, 20:46-21:54.
- [ ] C12. Store engine freezes in the database, not files/config.
  Source: note 4, 21:54-22:12.
- [ ] C13. In quota status collection, do not inline complex values in
  a dictionary. Hoist each provider status to a named local such as
  ZAI status, quota status, Codex status, Copilot status.
  Source: note 4, 22:25-23:02.
- [ ] C14. Move quota check and quota error label logic to Heru or a
  dedicated engine/quota domain module if it is not CLI interaction.
  Source: note 4, 23:12-23:38.
- [ ] C15. In `pipeline_cli`, get persistence/store through the
  container or a lightweight sub-container instead of constructing
  `SQLitePersistence` inline.
  Source: note 4, 23:45-24:25.
- [ ] C16. Replace one-line chained construction in `pipeline_cli`
  with named locals: workspace, persistence/store, then method call.
  Source: note 4, 24:37-25:21.
- [ ] C17. Review state rendering in `pipeline journal`; split the
  many `if` branches into focused functions or object behavior.
  Source: note 4, 25:41-26:19.

## Pool CLI Instructions

- [ ] P1. Review `pool.py`; it is around 400 lines and likely should
  be split.
  Source: note 4, 26:24-26:32.
- [ ] P2. Clarify what `pool` is as a domain concept.
  Source: note 4, 26:32-26:40.
- [ ] P3. Replace functions that simply return dictionaries with
  dataclasses.
  Source: note 4, 26:40-27:12; note 4, 31:15-31:23.
- [ ] P4. Move task-status business logic out of pool CLI, for
  example checks like "if task status not in interrupted pipeline".
  Source: note 4, 27:27-28:20.
- [ ] P5. Add domain methods/properties such as `task.is_resumable`
  and possibly `task.is_closed`, instead of repeating status-set
  checks in CLI.
  Source: note 4, 27:40-28:56.
- [ ] P6. Hoist complex inline expressions such as
  `entry.get("stage_outcomes", ...)` into named locals.
  Source: note 4, 29:03-29:33.
- [ ] P7. Replace pool stop condition labels with an enum/domain
  value. Do not maintain ad hoc string labels in CLI.
  Source: note 4, 29:33-30:19.
- [ ] P8. Use a simple value such as `single_task_complete` instead
  of over-complicated label manipulation.
  Source: note 4, 30:19-30:30.
- [ ] P9. Replace tuple results around block/remind/no-useful-progress
  data with one dataclass containing those fields.
  Source: note 4, 30:35-31:00.
- [ ] P10. Ensure type checking fails when functions return values but
  their annotations say they return nothing. Specific pool areas
  mentioned: root/completed/flagged typing around lines 261-276.
  Source: note 4, 31:26-32:23.
- [ ] P11. Remove `isinstance` checks and dictionaries in pool reports
  by returning normal report objects.
  Source: note 4, 32:30-33:06.

## Process Profile And Config Instructions

- [ ] F1. Review `config/profiles/defaults.py`. Default process
  profiles should probably be YAML files, not code dictionaries.
  Source: note 4, 33:30-34:30.
- [ ] F2. Replace profile dictionaries with a class/dataclass or
  Pydantic model loaded from YAML with validation.
  Source: note 4, 33:43-34:07; note 4, 35:45-36:08.
- [ ] F3. Remove unnecessary assignments around the early defaults
  lines called out near lines 19-27.
  Source: note 4, 34:30-35:03.
- [ ] F4. Simplify `resolve_process_profile`: lookup by name, load if
  present, otherwise use default. Remove unnecessary extra structure.
  Source: note 4, 35:03-35:23.
- [ ] F5. Remove `from_dict` if Pydantic/model validation can load the
  model directly.
  Source: note 4, 35:45-36:08.
- [ ] F6. Fix `config/profiles/rendering.py` docstrings. They must
  explain what renders what and why.
  Source: note 4, 36:15-36:49.
- [ ] F7. Move some `config/paths.py` responsibilities to
  `Workspace`; the container should resolve LiteHive root, construct
  `Workspace`, then ask `Workspace` for paths.
  Source: note 5, 00:01-00:22.
- [ ] F8. Review `registry` mutex/busy-timeout/lock-retry behavior and
  explain why it exists.
  Source: note 5, 00:35-00:46.
- [ ] F9. Remove registry global/function-as-global access. Use
  dependency injection.
  Source: note 5, 00:46-01:14.
- [ ] F10. Split registry responsibilities. Config, loading,
  persistence queries, sqlite details, security, and workspace path
  registration should not all live in one mixed module.
  Source: note 5, 01:22-02:21.
- [ ] F11. Simplify `register_workspace_path`; verify whether its
  complexity is justified.
  Source: note 5, 02:12-02:25.

## Runtime Settings And Engine Model Instructions

- [ ] R1. Review runtime setting keys. The note questions why the
  current key machinery exists.
  Source: note 5, 02:25-02:37.
- [ ] R2. Remove duplicate config layer logic such as
  `read_config_layer` and `merge_config_layers` if config loading
  already owns it.
  Source: note 5, 02:37-02:53.
- [ ] R3. Replace custom `json_dump` / `json_load` helpers with typed
  serialization via Pydantic or another model layer.
  Source: note 5, 02:53-04:35.
- [ ] R4. Runtime setting values and contexts should be normal typed
  objects or strings, not broad `Mapping`/`Any` payloads.
  Source: note 5, 04:09-04:35.
- [x] R5. Remove `set_engine_preference` if it only delegates to
  `set_runtime_setting`.
  Source: note 5, 04:44-05:12.
  Reviewed 2026-05-07: kept `set_engine_preference` because it does
  not only delegate; it normalizes and validates the engine sequence
  with `normalize_engine_sequence(...)` before the audited write.
  Removing it would push that validation back into CLI callers or
  allow malformed preference rows into the audit log.
- [x] R6. Audit default arguments in runtime settings. If callers
  always pass values, remove defaults so values are explicit.
  Source: note 5, 05:15-05:40.
  Completed 2026-05-07: audited runtime-setting wrapper callers and
  removed unused defaults for `actor`, `source`, and `context` from
  `set_default_engine`, `set_engine_preference`, `set_engine_freeze`,
  and `clear_engine_freeze`.
- [x] R7. Simplify `clear_engine_freeze`: it should perform the small
  database mutation/audit needed and nothing more.
  Source: note 5, 05:55-06:25.
  Completed 2026-05-07: `clear_engine_freeze` now updates the
  `engine_freeze` row and audit log directly after bootstrap, instead
  of loading all runtime settings and routing back through
  `set_runtime_setting`.
- [x] R8. In `engine_models`, remove engine attempt order
  deduplication. Assume users provide the right order.
  Source: note 4, 36:49-37:16.
  Completed 2026-05-07: removed selection-layer dedupe from
  `_engine_attempt_order` and explicit `engine_names` handling.
- [x] R9. Add dataclass and field docstrings for engine-related
  dataclasses such as `EngineSkip`.
  Source: note 4, 37:22-37:49.
  Completed 2026-05-07: added class and attribute documentation for
  `EngineSkip`, `EngineSelection`, and `EngineQuotaBlock`.
- [ ] R10. Remove unnecessary `None` fields from engine model
  dataclasses.
  Source: note 4, 37:49-37:56.
- [x] R11. Move parse helpers such as `parse at time you receive`,
  `parse_engine_freeze_until`, and possibly quota parsing to a
  utility or Heru/engine-owned module.
  Source: note 4, 37:56-38:18.
  Completed 2026-05-07: moved freeze-date parsing and shared UTC
  timestamp parsing into `litehive.config.time_parsing`; engine
  selection and quota handling now import the shared helpers instead of
  carrying duplicate private parsers. Searched for a separate
  "parse at time you receive" helper and found no code hit. Verified
  with focused pyrefly and `uv run pytest tests/config/test_engine_freeze.py -q`.
- [x] R12. Remove YouTube engine names if they are still present.
  Source: note 4, 38:18-38:25.
  Verified 2026-05-07: searched `litehive`, `tests`, and `docs`
  excluding source-recording artifacts for `youtube`, `YouTube`,
  `yt-`, `yt_`, and `YT`. No engine-name references remain; the
  only hit was this checklist item.
- [ ] R13. Move active engine freeze out of config and into database
  runtime state.
  Source: note 4, 38:25-39:13.
- [x] R14. Replace `getattr(status, ...)` in engine models with typed
  status objects.
  Source: note 4, 39:30-40:21.
  Completed 2026-05-07: `engine_models` now types quota probes through
  a quota-status protocol and reads status fields directly. Verified with
  `uv run pyrefly check litehive/config/engine_models.py` and
  `uv run pytest tests/config/test_engine_freeze.py -q`.
- [x] R15. Move quota handling into a separate module.
  Source: note 4, 40:21-40:31.
  Completed 2026-05-07: moved Heru quota probe dispatch and quota
  block translation into `litehive.config.engine_quota`; engine
  selection imports only `engine_quota_block`. Verified with
  `uv run pyrefly check litehive/config/engine_quota.py
  litehive/config/engine_models.py tests/config/test_engine_freeze.py`
  and `uv run pytest tests/config/test_engine_freeze.py -q`.
- [x] R16. Replace `engine_quota_block` tuple returns with a dataclass
  containing reason string and datetime. Optional absence can still be
  modeled, but the present value should not be a tuple.
  Source: note 4, 41:17-41:40.
  Completed 2026-05-07: `engine_quota_block` now returns
  `EngineQuotaBlock | None`; selection reads `reason` and
  `freeze_until` fields instead of tuple-unpacking. Verified with
  focused pyrefly and `uv run pytest tests/config/test_engine_freeze.py -q`.
- [ ] R17. Simplify `select_engine`; it has too many parameters and
  too much branching.
  Source: note 4, 41:58-42:28.
  Progress 2026-05-07: extracted candidate engine order construction
  into `_candidate_engine_order`, keeping explicit `engine_names`,
  plan-based fallback ordering, and excluded-engine filtering out of
  the main selection loop. Remaining work: split freeze/quota/
  availability handling or move the selection request shape into a
  small object.
- [ ] R18. Move engine lookup/order logic into config/domain object
  methods where appropriate, for example `config.get_engine(...)`.
  Source: note 4, 42:28-43:05.
- [ ] R19. Clarify `model_override`; if a model should always be set,
  make it always explicit.
  Source: note 4, 43:05-43:36.
- [ ] R20. Simplify `resolve_engine_name`,
  `resolve_engine_attempt_order`, and `resolve_engine_plan`. The flow
  should load engine names, choose the first unfrozen engine,
  instantiate it, and stop.
  Source: note 4, 43:40-44:29.
- [ ] R21. Move recovery-stage and hijackable-stage logic out of
  config into domain types such as `TaskStage` / `TaskRecord`.
  Source: note 4, 44:29-45:49.
- [ ] R22. Ensure each stage knows its agent/role and each agent/role
  knows its stages. Do not scatter role/stage dictionaries.
  Source: note 4, 45:49-46:13.
- [ ] R23. Split `config/engine_models.py`; it is too large.
  Source: note 4, 46:23-46:29.
- [ ] R24. Desired config loading shape: load YAML, validate into a
  config object, and use that object. Avoid hand-written validation
  where Pydantic can own it.
  Source: note 4, 46:29-48:13.
- [ ] R25. Prefer `Workspace.load_config()` or an equivalent workspace
  service over root-path config loading.
  Source: note 4, 46:49-47:10.
- [ ] R26. Remove `is_iterable`/`Mapping`-style validation in config
  models when Pydantic can enforce the shape.
  Source: note 4, 47:21-48:13.
- [ ] R27. Pydantic should load proper enums directly so the project
  does not maintain duplicate string lists.
  Source: note 4, 47:45-48:13.

## Workspace Instructions

- [ ] W1. Remove global variables from `workspace_files` and related
  workspace modules.
  Source: note 5, 06:32-06:45.
- [ ] W2. Review and likely remove
  `workspace.config.template.unresolved.shell.variable`; the note
  questions why it exists.
  Source: note 5, 06:45-06:54.
- [ ] W3. Loading a workspace should not create a workspace. If a path
  is not an existing LiteHive project, raise an error.
  Source: note 5, 07:33-08:59.
- [ ] W4. Remove `workspace_parent_root` / upward parent search.
  Source: note 5, 08:16-08:51.
- [ ] W5. Remove `task_matches` / `task is none or task exists` style
  helpers; they obscure real branches.
  Source: note 5, 09:03-09:23.
- [ ] W6. Rewrite the complex list comprehension around the line
  called out near 134.
  Source: note 5, 09:31-09:43.
- [ ] W7. Review `path inside managed workspace`; likely remove nested
  workspace rejection logic unless it is truly needed.
  Source: note 5, 09:47-10:32.
- [ ] W8. Simplify `normalize_workspace_root`; it may only need to
  resolve the path and check whether `.litehive` exists.
  Source: note 5, 10:53-12:23.
- [ ] W9. Remove legacy task-existence checks now that tasks are in
  the database.
  Source: note 5, 12:23-12:30.
- [ ] W10. Remove `resolve_workspace_from_search_root` / search repo
  behavior. If no workspace is passed, use current directory and fail
  if it is not a LiteHive project.
  Source: note 5, 12:39-13:25.
- [ ] W11. Remove global workspace registry behavior. The note says
  the global registry of workspaces is no longer wanted.
  Source: note 5, 13:27-13:41.
- [ ] W12. Replace `ensure_workspace` with explicit
  `create_workspace` / bootstrap behavior. Do not accidentally create
  workspace state during load.
  Source: note 5, 13:43-14:18.

## Daemon Instructions

- [ ] DM1. Move heartbeat/stop and related daemon parameters into
  config.
  Source: note 5, 14:21-14:36.
- [ ] DM2. Remove unclear `continue, None, None` stop-reason logic.
  Model stop reasons explicitly.
  Source: note 5, 14:37-14:58.
- [ ] DM3. Move `check_origin_divergence` to git-owned code.
  `halt_for_origin_divergence` may remain daemon-owned if it is the
  daemon reaction.
  Source: note 5, 14:58-15:23.
- [ ] DM4. Review output-stream usage in daemon functions; output
  stream should not leak through unrelated daemon logic.
  Source: note 5, 15:23-15:42.
- [ ] DM5. Remove useless retry/restart code around line 133 if it is
  dead weight.
  Source: note 5, 15:48-16:04.
- [ ] DM6. Document `sleep_with_stop`: why it exists, where it is
  called, and how stop requests enter it.
  Source: note 5, 16:04-16:18.
- [ ] DM7. Add proper callable typing for stop request function
  parameters; type checker should reject missing types.
  Source: note 5, 16:17-16:31.
- [ ] DM8. Replace `append_attention_log` free/global behavior with
  an `AttentionRepository` that writes to the database.
  Source: note 5, 16:34-16:56.
- [ ] DM9. Replace daemon status snapshot tuple/dict return with a
  normal object. If `collect_task_pipeline_status` already returns a
  good object, return that instead of converting to dict.
  Source: note 5, 17:00-17:42.
- [ ] DM10. Fix daemon docstrings to say where each helper is used
  and why.
  Source: note 5, 17:43-17:48.
- [ ] DM11. Review `pick_default_command_prefix`; the note questions
  whether it is really needed.
  Source: note 5, 17:50-18:01.
- [ ] DM12. If `uv` is used to run LiteHive as an executor in another
  project, ensure it runs in the correct project directory.
  Source: note 5, 18:28-18:56.
- [ ] DM13. Replace `emit(..., stream=...)` free functions with an
  object that takes the stream in its constructor and exposes methods.
  Source: note 5, 19:00-19:27; note 5, 21:25-21:52.
- [ ] DM14. Replace daemon health check dictionaries with typed
  entries.
  Source: note 5, 19:28-19:34.
- [ ] DM15. Remove wrapper functions like `clear_recorded_daemon` /
  `unregister_daemon` if they only call another function.
  Source: note 5, 19:53-20:07.
- [ ] DM16. Split daemon termination behavior into submodules/classes
  if it violates single responsibility.
  Source: note 5, 20:19-20:42.
- [ ] DM17. Fix type checking around "runner is live" and `has_work`;
  functions should accept and return normal typed objects.
  Source: note 5, 20:43-21:07.
- [ ] DM18. Convert `should_continue_for_stop_reason` to use a domain
  enum/object rather than arbitrary object/string values.
  Source: note 5, 21:08-21:24.
- [ ] DM19. Add a daemon DI container or daemon-specific container
  instead of global variables.
  Source: note 5, 21:52-22:06.
- [ ] DM20. Document `maybe_run_workspace_backup`: where it is called
  and whether it runs before any operation that mutates LiteHive
  state.
  Source: note 5, 22:17-22:40.
- [ ] DM21. Consider moving workspace backup out of daemon if it is a
  general pre-operation concern.
  Source: note 5, 22:40-22:50.
- [ ] DM22. Simplify `run_daemon_loop`; it is complex and should be
  decomposed.
  Source: note 5, 22:57-23:16.
- [ ] DM23. Model daemon execution as an object such as
  `DaemonExecutor` or `DaemonWorkspace` with methods like start/stop,
  instead of passing workspace strings and loose helpers.
  Source: note 5, 23:18-24:45.
- [ ] DM24. Consider a `DaemonLogs` class with log-related methods.
  Source: note 5, 24:47-25:10.
- [ ] DM25. Replace daemon metadata dictionaries with dataclasses.
  Source: note 5, 25:16-25:35.
- [ ] DM26. Consider a `WorkspaceDaemon` object that manages daemon
  registration, lookup, and workspace daemon behavior.
  Source: note 5, 25:29-26:11.

## Database And Migrations Instructions

- [ ] DB1. Move database migrations into separate migration modules,
  matching the fact that there are now several migrations.
  Source: note 5, 26:28-27:37.
- [ ] DB2. Clarify whether helper data such as task intent column
  values is migration-only schema code or business logic. Document
  where it is used.
  Source: note 5, 26:52-27:19.
- [ ] DB3. If schema/migration code is large, split it so migrations
  are easier to read and audit.
  Source: note 5, 27:19-27:37.

## Type Checker And Guardrail Instructions

- [ ] T1. Make the type checker catch functions with missing return
  annotations when they return values.
  Source: note 4, 31:26-32:03.
- [ ] T2. Make the type checker catch untyped variables/parameters
  like root/completed/flagged in the pool code.
  Source: note 4, 32:03-32:23.
- [ ] T3. Make the type checker catch daemon `runner_is_live` /
  `has_work` object misuse.
  Source: note 5, 20:43-21:07.
- [ ] T4. Add a ruff/custom guardrail for `getattr`.
  Source: note 4, 40:31-40:58.
- [ ] T5. Add or strengthen guardrails against dictionaries and
  untyped `object` payloads crossing domain boundaries.
  Source: note 3, 27:53-28:24; note 4, 07:55-08:23; note 4,
  40:00-40:21.

## Execution Discipline Instructions

- [ ] X1. Do not report "done" until the exact old comments and old
  code paths have been checked. The notes repeatedly say prior
  feedback was claimed fixed while the same code remained.
  Source: note 4, 02:17-02:49.
- [ ] X2. Revisit all prior notes and turn every small detail into a
  separate task.
  Source: note 4, 10:21-10:34.
- [ ] X3. Generalize the concrete feedback into durable style/domain
  rules, then apply those rules across the codebase.
  Source: note 3, 32:48-33:08; note 6, 06:09-06:17.
- [ ] X4. Do not batch all of this into one broad refactor. Add
  characterization tests before structural moves, and keep tests green
  between slices.
  Source: repo AGENTS instructions and repeated voice-note process
  feedback.
