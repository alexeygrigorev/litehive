# Code Fixes Plan

This document is a planning log for concrete code fixes and refactors that have
been identified but not yet implemented.

Rules for this document:

- planning only; do not treat items here as already fixed
- each section should capture the question or problem statement
- each action list should be concrete enough to execute later without
  rediscovery
- note open questions, risks, and validation steps when they matter

## Intake

Questions and follow-up actions from the current planning session will be added
below.

## Recovery Policy: same-stage crash budget

Question:

- Desired behavior:
  - crash -> recovery attempt -> continue when recovery succeeds
  - if the next failure happens in a different stage, allow recovery again
  - if the next failure happens in the same stage, mark failed
- Why couldn't the recovery agent fix it?

Current code shape:

- The current lifecycle is already close to this policy.
- `litehive/lifecycle/rules.py` routes `Crash` and `Timeout` from normal stage
  phases to `recovering`.
- `litehive/roles/recovery.py` gives the recovery agent one verdict:
  - `resume` / `advance` / `done` -> success
  - anything else -> recovery failed
  - `budget_hit` -> explicit recovery budget failure
- `litehive/lifecycle/orchestration.py` then uses
  `task.runtime.last_crashed_stage` to implement a same-stage crash circuit
  breaker:
  - first crash in stage X records `last_crashed_stage = X`
  - if the task later crashes again in the same origin stage X after recovery,
    the task is flagged with `crash_budget_exhausted`
  - if the task advances out of that crash path, the marker is cleared so a
    later crash in a different stage gets a fresh recovery attempt

Answer:

- The intended policy is reasonable and mostly matches the current
  implementation.
- The weak point is not the high-level rule. The weak point is observability
  and state modeling.
- Right now "recovery agent couldn't fix it" is not represented as a structured
  diagnosis. In practice it means one of these happened:
  - the recovery agent returned a non-success verdict
  - the recovery agent crashed or timed out
  - the task resumed, but then crashed again in the same origin stage, so the
    circuit breaker concluded recovery did not eliminate that failure class
- The system does not currently persist a first-class explanation of why
  recovery was ineffective beyond free-text reason/message and logs.

Concrete actions:

- Replace the split same-stage crash tracking logic with one explicit recovery
  budget model owned by lifecycle state, instead of dividing meaning between
  `state.recovery_attempt[...]` and `task.runtime.last_crashed_stage`.
- Introduce explicit domain types for recovery bookkeeping instead of loose
  string fields:
  - `TriggerEventKind(str, Enum)` for normalized persisted trigger labels
  - `RecoveryTrigger` for origin stage + trigger event kind + optional
    diagnostics
  - `RecoveryOutcome` for one persisted recovery attempt/result
- Define the policy in one place:
  - one recovery attempt per origin stage failure class
  - a new origin stage gets a fresh budget
  - the same origin stage only gets another recovery if the failure fingerprint
    is meaningfully different and we explicitly decide that is allowed
- Persist structured recovery outcome data after each recovery run:
  - triggering origin stage
  - triggering event kind (`crash`, `timeout`, `blocked`, `retry_limit`, etc.)
  - failure fingerprint/classification
  - recovery verdict
  - recovery reason
  - whether the task resumed, advanced, or terminated
- Define and persist a failure fingerprint for crash-budget decisions rather
  than relying only on stage name.
- Add a dedicated explanation field for terminal recovery failures so `status`,
  `pipeline journal`, and reports can answer "why recovery could not fix this"
  without reading free-form logs.
- Review whether `RecoveryFailed` should always mean terminal failure, or
  whether some recovery verdicts should route back with an explicit operator
  attention state instead of collapsing into generic failure.
- Add focused lifecycle tests for:
  - crash in stage A -> recovery succeeds -> crash again in stage A -> failed
  - crash in stage A -> recovery succeeds -> advance to stage B -> crash in
    stage B -> recovery allowed
  - recovery agent returns failed verdict -> task flagged with structured
    reason
  - recovery agent crashes/timeouts -> terminal reason is explicit and
    distinguishable

## Domain Vocabulary: classes and enums

Question:

- Eventually most of these concepts should live under `litehive/domain/`
- Define vocabulary in terms of Python classes, not just prose
- Use enums for states instead of `Literal[...]`

Answer:

- The codebase already partly points this way:
  - lifecycle events are dataclasses in `litehive/lifecycle/events.py`
  - some concepts are real enums in `litehive/lifecycle/types.py`
  - but many state-like vocabularies in `litehive/domain/common.py` are still
    `Literal[...]` aliases
- The target domain model should make the distinction explicit:
  - event objects are classes
  - persisted state categories are enums
  - structured persisted records are dataclasses or Pydantic models
- For the recovery question specifically, `runtime event` is not the right
  term. Use:
  - `LifecycleEvent` for the in-memory event class hierarchy
  - `TriggerEventKind` for the normalized stored enum value attached to
    recovery bookkeeping

Concrete actions:

- Add canonical domain types for the major vocabularies currently described in
  `docs/vocabulary.md`, including:
  - `TaskStage`
  - `LifecyclePhase`
  - `PipelineStatus`
  - `TaskStatus`
  - `RunnerStatus`
  - `Verdict`
  - `TriggerEventKind`
  - `RecoveryDisposition`
- Replace `Literal[...]` aliases in `litehive/domain/common.py` with enums for
  state-like categories, starting with:
  - `TaskStatus`
  - `PipelineStatus`
  - `RunnerExecutionStatus`
  - `OutcomeKind`
  - `OutcomeReasonCode`
- Decide whether Heru-owned `SubagentStatus` should also become an enum on the
  Heru side instead of a compatibility literal.
- Introduce structured record types for persisted recovery data:
  - `RecoveryTrigger`
  - `RecoveryOutcome`
  - optional `FailureFingerprint`
- Normalize naming between current runtime/lifecycle types and the target
  domain model:
  - keep `Event` as an implementation detail or rename it to
    `LifecycleEvent`
  - keep persisted normalized labels in `*Kind` / `*Status` enums
  - do not reuse one type for both a rich event object and a stored label
- Migrate CLI/help/docs/code comments to the enum/class terminology after the
  domain types exist.
- Add compatibility helpers only where needed for SQLite serialization and
  legacy string-based data, then remove call-site string literals over time.

## Domain Vocabulary: overloaded terms cleanup

Question:

- The codebase overloads `status`, `stage`, `phase`, `outcome`, `reason`,
  `report`, `thread/comment`, `trigger`, `journal`, and `log`
- We want vocabulary grounded in actual Python classes and enums that can move
  under `litehive/domain/`

Answer:

- Direct file reads across `domain/`, `lifecycle/`, `cli/`, `tasks/`,
  `state/`, and `recovery/` show the same pattern:
  - `status` currently spans task, pipeline, runner, stage-runtime, subagent,
    worktree-rescue, quota, and pool-stop concepts
  - `stage` is used both for business workflow stages and for full lifecycle
    machine states
  - `reason` sometimes means free-text message and sometimes means normalized
    machine code
  - `outcome` spans runtime outcome, explicit task-close disposition, and
    recovery result
  - `report` spans stage verdict records, recovery summaries, YAML artifacts,
    and thread-backed verdict submissions
- The vocabulary should split these into explicit domains:
  - machine state enums
  - business-stage enums
  - event class hierarchy
  - persisted result enums
  - structured records for discussion, recovery, and rescue results

Concrete actions:

- Add and standardize the following domain types:
  - `PipelineState`
  - `CoreStage` or `TaskStage`
  - `LifecyclePhase`
  - `StageRunStatus`
  - `TaskCloseOutcome`
  - `WorktreeRescueOutcome`
  - `PoolStopReason`
  - `DiscussionThread`
  - `DiscussionEntry`
  - `VerdictEntry`
  - `RecoveryContext`
  - `JournalEntry`
- Decide whether `TaskRecord.pipeline_status` should eventually become
  `pipeline_state` or remain a projected `pipeline_status` while lifecycle
  storage owns `PipelineState`.
- Rename stage-like fields currently called `step` to `stage` where they
  really carry business-stage labels, including hotspots in:
  - `RuntimeStageState.step`
  - `StageReport.step`
  - `RuntimeContinuationHandoff.step`
  - CLI `agent report --step`
- Reserve `reason` for machine-readable codes only if we adopt that rule; if
  so, rename human-text fields to `message` or `rationale` across:
  - lifecycle events
  - task close / operator actions
  - pool/worktree CLI outputs
  - recovery bookkeeping
- Split `TaskOutcomeKind` from `TaskCloseOutcome` so explicit operator close
  dispositions are not conflated with runtime outcome categories.
- Replace generic rescue/worktree string statuses with a dedicated
  `WorktreeRescueOutcome` enum and rename printed CLI fields to
  `rescue_status`.
- Replace generic `trigger` fields in recovery/report models with
  `trigger_event_kind`, and replace `stage` with `origin_stage` where the field
  means the stage that triggered recovery.
- Rename `failure_context` to `recovery_context` if the payload is only used as
  recovery-entry context.
- Distinguish `journal`, `log`, and `transcript` in both CLI help and model
  names:
  - `journal` for structured history
  - `log` for raw operational/debug output
  - `transcript` for rendered subagent conversation/event trace
- Remove remaining user-facing `v2` wording while touching CLI help so the new
  vocabulary lands together instead of mixing old and new labels.

## Vocabulary Open Questions from Review

Question:

- The current draft still contains too many alternatives, unclear distinctions,
  and unexplained domain words
- The vocabulary doc should choose one name per concept and explain why

Answer:

- The review comments point to a few concrete gaps in the current draft:
  - some entries still say `X or Y` instead of selecting one canonical term
  - some domain words are introduced without defining what the underlying
    object is, especially `record`, `runtime`, `engine`, `subagent`,
    `transcript`, `timeline`, and `journal`
  - `stage`, `phase`, `state`, and `pipeline` are still too easy to confuse
  - verdict terminology is still unsettled, especially whether `pass` should
    become `accept`, whether `blocked` is distinct from `reject`, and whether
    `comment` belongs in the verdict enum at all
  - the current doc lists enum names but does not yet show the actual canonical
    values and short descriptions in a consistent format

Concrete actions:

- Revise `docs/vocabulary.md` so every concept chooses exactly one canonical
  term. Remove unresolved `or` wording such as:
  - `CoreStage` or `TaskStage`
  - `DiscussionEntry` or `TaskDiscussionComment`
  - other places where multiple alternatives are still shown
- Add a dedicated section defining the relationship between:
  - `engine`
  - `subagent`
  - `subagent run`
  - `session`
  so the execution model is explicit
- Add a dedicated section defining the relationship between:
  - `task`
  - `task record`
  - `task runtime`
  - lifecycle `TaskState` / `PipelineState`
  so `record`, `runtime`, and `state` stop overlapping
- Add a dedicated section explaining the lifecycle naming stack end-to-end:
  - `task stage`
  - `lifecycle phase`
  - `pipeline state`
  - `lifecycle node`
  and explicitly decide which of those should appear in CLI output
- Standardize enum presentation in `docs/vocabulary.md`:
  - show one canonical enum name
  - list actual values
  - add a one-line description for each value where ambiguity exists
- Review whether `pipeline` and `lifecycle` should both remain public-facing.
  If not, pick one umbrella word and demote the other to implementation-only
  language.
- Review verdict vocabulary and make a single explicit decision on:
  - `pass` vs `accept`
  - whether `blocked` is a real verdict distinct from `reject`
  - whether `comment` is a verdict, an entry type, or a separate field
- Review whether `discussion thread` is the right term at all for the task
  append-only history. Compare alternatives such as:
  - `task activity`
  - `task feed`
  - `task timeline`
  - `task entries`
- Define `FailureFingerprint` more concretely or rename it if another term is
  clearer, for example:
  - `FailureSignature`
  - `FailureKey`
  - `FailureClassifier`
- Revisit artifact vocabulary:
  - if `transcript` is too audio-oriented, choose a clearer term
  - define exactly what `timeline` means and whether it should instead be
    `event stream`
  - define how `journal` differs from the task-entry history

## Selected Vocabulary Decisions

These are now chosen in `docs/vocabulary.md` and should be treated as the
current target vocabulary unless explicitly revised again.

- Use `TaskStage` for user-facing major work steps.
- Use `StagePhase` for `before` / `active` / `after`.
- Use `PipelineState` for the full machine-state enum.
- Use `LifecycleNode` for the executable implementation object.
- Use `LifecycleEvent` for transition-triggering event classes.
- Use `RecoveryTrigger` and `trigger_event_kind` for persisted recovery-entry
  context.
- Use `TaskActivity` and `ActivityEntry` instead of `discussion thread` and
  `discussion entry`.
- Use `StageVerdict` with values `accept`, `reject`, and `blocked`.
- Treat `comment` as an entry type or message, not a stage verdict.
- Use `TaskCloseOutcome` for explicit close dispositions.
- Use `ExecutionTrace` instead of `transcript` in new naming.
- Use `EventStream` instead of `timeline` in new naming.
- Use `LifecycleJournal` for machine-generated transition history and keep it
  distinct from task activity.

## Vocabulary Structure Decisions

The vocabulary doc is now organized by domain instead of by scattered concept
type so related classes and enums can be reviewed together.

- `Workspace Domain`
- `Task Domain`
- `Pipeline Domain`
- `Recovery Domain`
- `Execution Domain`
- `Activity and Reports Domain`
- `Artifacts Domain`
- `Configuration Domain`

Follow-up actions:

- Keep future vocabulary additions grouped into one of those domains rather than
  reintroducing cross-cutting scattered sections.
- Revisit the following still-open simplification questions in-place within
  their domains:
  - whether `PipelineStateView` should exist separately from `PipelineState`
  - whether `TaskStatus` should collapse further
  - whether `StagePhase` should exist at all
  - whether `RecoveryResult` is the final name

## engine_models.py feedback

Question:

- `litehive/config/engine_models.py` does not seem like a config module
- datetime parsing helpers look overbuilt
- dedupe logic looks suspicious
- freeze checks parse repeatedly
- `active_engine_freezes()` feels like runtime state, possibly SQLite-backed
- `_engine_quota_block()` is branch-heavy
- `workspace_model_for_engine()` and `resolve_model()` look awkward
- explain what the remaining code is actually for

Answer:

- The main critique is valid: this file mixes three concerns that should not be
  in one module:
  - user/workspace configuration access
  - runtime engine availability and quota decisions
  - continuation/recovery handoff behavior
- Because those concerns are mixed, the code has helper functions that exist
  mostly to glue mismatched representations together rather than express the
  domain cleanly.

What looks unnecessary or misplaced:

- `litehive/config/engine_models.py` is a poor home for runtime engine
  selection and continuation logic.
- `_parse_datetime_utc()` exists because freeze values and quota reset values
  are currently handled as loose strings from multiple sources. That is a code
  smell, not a strong design.
- `parse_engine_freeze_until()` exists only because the CLI accepts a
  user-facing date and the persisted config currently stores an ISO timestamp.
  If freeze state moves to a typed store, this helper can likely disappear or
  become a tiny CLI parser.
- `_dedupe_engine_names()` is only defensible for ad hoc candidate lists passed
  by callers. It should not be compensating for duplicates in normalized config.
- `is_engine_frozen()` and `active_engine_freezes()` reparsing strings means the
  state is stored in the wrong shape for the access pattern.
- `active_engine_freezes()` does look more like runtime state than stable
  config, especially when freezes are written automatically in response to
  quota checks.
- `_engine_quota_block()` should be engine-specific adapter logic instead of a
  branching dispatcher.
- `workspace_model_for_engine()` is a symptom of the config schema using one
  field per engine instead of a normalized engine-settings model.

What is still needed conceptually:

- engine attempt ordering is needed
  - something must combine the task's initial engine choice with workspace
    engine preference/fallback order
- engine selection is needed
  - something must choose an eligible engine after considering availability,
    freeze state, and quota state
- model resolution is needed
  - something must define precedence between explicit override, task-specific
    model pin, and workspace default for the selected engine
- continuation handoff persistence is needed
  - when switching engines or resuming after interruption, the runtime needs a
    structured record of what the next agent should inherit
- role selection for recovery-aware stages is needed
  - implementing/testing/accepting can run under `recovery` semantics when the
    task is continuing from a failed prior run

Concrete actions:

- Split `litehive/config/engine_models.py` into separate modules with clear
  ownership, for example:
  - config parsing / config access
  - engine selection and availability policy
  - quota/freeze policy
  - continuation handoff persistence
- Move runtime selection code out of `config/`.
- Decide whether engine freeze is operator config or runtime state:
  - if it is operator intent, keep it in workspace config and stop mutating it
    automatically from quota checks
  - if it is runtime state, move it to SQLite/runtime store and expose it via
    status/CLI from there
- Stop storing freeze timestamps as free-form strings inside the working model.
  Parse once at load boundaries and keep typed UTC datetimes internally.
- Replace generic datetime parsing with source-specific parsing at the adapter
  boundary:
  - workspace config parser handles operator freeze input
  - each quota adapter parses its own reset timestamp format
- Remove `_dedupe_engine_names()` from the hot path unless the caller is truly
  passing an untrusted ad hoc list. Prefer validating candidate construction at
  the source.
- Replace `_engine_quota_block()` with an engine adapter table/protocol so each
  engine owns:
  - quota probe
  - reset-time parsing
  - blocked reason rendering
  - optional monitoring side effects
- Normalize model config shape so model lookup is data-driven instead of a long
  `if engine_name == ...` chain.
- Rename and document model precedence explicitly. Current meaning of
  `resolve_model()` is:
  - return `None` if the engine does not support model override
  - otherwise prefer explicit `model_override`
  - otherwise prefer `task.model`
  - otherwise use the workspace default for that engine
- Decide whether task-level `model` is really engine-agnostic. If not, replace
  it with engine-scoped model state so a model pin from one engine does not
  silently bleed into another engine selection.
- Add tests around:
  - engine preference order
  - same-engine and cross-engine fallback
  - freeze persistence / lookup semantics
  - per-engine quota blocking
  - model precedence resolution

## agents/: Heru boundary, manager cleanup, and dead prompt path

Question:

- `litehive/agents/unified_events.py` looks like Heru code
- the Heru delegation wrapper in `agents/_continuation.py` is an anti-pattern
- remove wrappers like that across the codebase
- `__all__` is not wanted in this codebase
- `agents/artifacts.py` and `prune_superseded_subagent_artifacts()` look suspicious
- `SubagentManager` should use dependency injection and `run()` is too large
- `SessionMixin` looks unnecessary
- `agents/parsing.py` is misnamed, uses inline imports, and still reads YAML
- `agents/prompts.py` may be dead; prompt logic should be unified
- `agents/sandbox.py` should likely move under a sandbox-owned module
- engine-name dispatch chains should be replaced with engine-owned behavior

Current code shape:

- `litehive/agents/unified_events.py` is a generic parser/view over Heru's
  unified JSONL event schema, not Litehive-specific logic.
- `litehive/agents/_continuation.py` wraps
  `heru.extract_engine_continuation(...)` and adds a Litehive-specific
  preference for parsing unified stdout first.
- `litehive/agents/prompts.py` appears dead in runtime terms:
  - `stage_prompt()` is defined but not referenced anywhere else in Litehive
  - the active v2 path is `RoleAgent.build_prompt()` ->
    `litehive/lifecycle/prompt_serializer.py`
- `_load_agent_md()` currently has no matching `.litehive/agents/*.md` files in
  this workspace.
- `SessionMixin` is only used by `SubagentManager`.
- `prune_superseded_subagent_artifacts()` is used only from
  `SubagentManager.run()` and still prunes legacy names like `stdout.log` /
  `stderr.log`.
- `tasks/reports.py` still stores discussion comments in YAML
  (`comments.yaml`), so the agent verdict/report path is not yet SQLite-backed.

Answer:

- `unified_events.py` does belong on the Heru side if Litehive intends to treat
  Heru as the owner of the unified event contract. The code is generic and does
  not reference Litehive task concepts.
- The thin `extract_engine_continuation()` wrapper in
  `agents/_continuation.py` is pure indirection and should be removed.
- The richer `extract_execution_continuation()` function is only justified
  because Litehive wants to prefer unified-event continuation IDs before
  adapter-specific fallback. That behavior should live in Heru, not in a
  Litehive shim.
- `__all__` is sparse in Litehive, not pervasive, but if the codebase policy is
  "no `__all__`", the current remaining uses should be removed consistently.
- `artifacts.py` is needed today because Litehive persists task-scoped prompt /
  transcript / stdout / stderr / timeline artifacts for subagent runs, and both
  `manager.py` and `session.py` call it.
- Most of `artifacts.py` is Litehive-owned persistence policy, not Heru:
  - task-scoped folders
  - Litehive artifact naming
  - Litehive retention/pruning decisions
  - Litehive-specific snapshot/report files
- `prune_superseded_subagent_artifacts()` looks like cleanup of historical
  artifact shapes rather than a core requirement. If the policy is "keep full
  per-subagent evidence" or "prune by a new explicit retention policy", this
  helper should go.
- `SubagentManager.run()` currently does much more than "run":
  - allocates subagent ids and folders
  - resolves engine + sandbox execution mode
  - persists task/subagent runtime state
  - writes session start/progress/finish artifacts
  - classifies failures
  - records telemetry/monitoring
  - parses final report material
  - prunes older artifacts
- `supports_live_execution(...)` and related Heru engine-detection helpers exist
  because Heru currently infers whether an adapter really supports or prefers
  `run_live()` by inspecting override behavior. That solves a real contract
  problem today, but the introspection itself is a smell and should be replaced
  with explicit adapter capabilities/preferred execution mode in Heru.
- `isinstance(engine, ExternalCLIAdapter)` checks in `manager.py` look
  redundant given current Heru `get_engine()` returns `ExternalCLIAdapter`
  instances from a typed registry.
- The `*` in `SubagentManager.__init__(..., *, execution_root=...)` is just a
  Python keyword-only separator. It is unrelated to the mixin.
- `_stream_offsets` is runtime bookkeeping for append-only stream log deltas
  (`session.py` appends only the newly-seen suffix of stdout/stderr). The name
  is too vague.
- `task.runtime.current_stage.step` reflects a deeply nested runtime model. The
  smell is real. The fix is not to flatten blindly, but to introduce a clearer
  stage accessor/view so callers stop reaching through nested runtime internals
  everywhere.

Concrete actions:

- Move generic unified-event parsing/view code from Litehive into Heru:
  - parsed execution view
  - transcript rendering from unified events
  - continuation extraction from unified events
  - timeline derivation from unified events
- After that move, delete Litehive wrappers in `agents/_continuation.py` and
  replace call sites with direct Heru APIs.
- Audit Litehive for wrapper-only re-export/helper functions and remove any that
  only rename or forward to Heru or another local module without adding domain
  behavior.
- Remove all Litehive `__all__` declarations and document that policy in code
  style guidance.
- Delete dead `litehive/agents/prompts.py` if it is confirmed unused after a
  final call-site audit. Do not keep a second prompt system beside
  `roles/* + lifecycle/prompt_serializer.py`.
- Remove `_load_agent_md()` and the `.litehive/agents/*.md` prompt extension
  path unless a concrete supported use case exists.
- Unify prompt ownership under the active v2 pipeline path:
  - prompt data assembly in role/lifecycle code
  - final text rendering in one serializer
  - no parallel prompt DSL in `agents/`
- Merge `SessionMixin` back into a concrete `SubagentSessionWriter` or directly
  into `SubagentManager`; avoid mixins for single-consumer helper code.
- Refactor `SubagentManager` to use injected collaborators instead of building
  them in `__init__`, for example:
  - config provider
  - engine resolver/factory
  - sandbox launcher/execution wrapper
  - artifact/session writer
  - runtime/task repository
  - monitoring/event recorder
- Split `SubagentManager.run()` into explicit phases/methods:
  - prepare subagent run context
  - choose execution strategy
  - execute or resume execution
  - classify execution result
  - persist runtime/session updates
  - record monitoring/events
  - finalize result
- Decide whether `run()` and `resume()` should be separate entrypoints instead
  of overloading `run(..., resume_session_id=...)`.
- Replace tuple/ternary-heavy finish-state plumbing with a dedicated result
  dataclass that carries:
  - execution
  - exit code
  - continuation
  - interruption reason
  - resource limit event
  - classified failure
- Extract the non-zero-exit branch in `SubagentManager.run()` into a dedicated
  classifier method.
- Rename `_stream_offsets` to something explicit like
  `session_stream_offsets_by_subagent`, or encapsulate it in the session writer.
- Replace `_report_step_for_task()` with a runtime-owned stage accessor or
  explicit stage argument passed by the caller. Avoid "guess a valid reporting
  stage from nested task runtime state" in manager code.
- Introduce clearer runtime accessors to reduce `task.runtime.current_stage.step`
  style call sites. Options:
  - `task.current_pipeline_stage`
  - `task.runtime.stage_name()`
  - a dedicated read model / stage view
- Rename `agents/parsing.py` to match what it does now, e.g.
  `report_resolution.py` or `stage_reports.py`.
- Split `stage_report_from_subagent()` into smaller responsibilities:
  - read latest submitted verdict for step
  - synthesize blocked report from resource limit
  - synthesize missing-verdict fallback
- Move inline imports to module top level unless they are proven cycle-breakers
  or optional heavy dependencies.
- Add Ruff enforcement for import-outside-top-level (`PLC0415`) and then fix or
  explicitly justify remaining exceptions.
- Migrate task discussion / verdict storage from YAML comments to SQLite, then
  update agent report resolution to read from SQLite instead of
  `comments.yaml`.
- Add `docs/vocabulary.md` defining the canonical terms for:
  - task comments / discussion / verdict entries
  - report vs thread/comment entry
  - subagent session / transcript / timeline / continuation
  - stage / phase / pipeline status
- Decide retention policy for subagent artifacts:
  - keep all per-subagent evidence
  - or prune by a clear retention policy
  - but remove legacy backward-compat pruning logic and old filenames
- Remove `prune_superseded_subagent_artifacts()` unless a current, documented
  retention requirement remains after that decision.
- Move external-engine sandboxing out of `litehive/agents/` into a sandbox-owned
  module, since it is infrastructure rather than agent orchestration.
- Replace engine-name `if/elif` dispatch chains with engine-owned metadata or a
  registry-driven policy object. Current hotspots include:
  - sandbox state-dir resolution
  - engine quota blocking
  - workspace model lookup

## CLI and workspace config cleanup

Question:

- `cli/templates/workspace_config.yaml` looks too large and maybe stale
- check whether `claude-opus-4.6` is correct
- engine-specific settings should be grouped
- strip config to the minimum necessary
- `VERDICT_ALLOWLIST` needs rationale comments
- drop unused/unsafe agent CLI overrides
- stop normalizing verdict aliases
- agent report path should not append YAML comments
- some option/default plumbing is hard to read

Answer:

- The workspace template currently exposes many defaults and implementation
  details. It reads more like an exhaustive config dump than a curated operator
  surface.
- As of April 15, 2026, Anthropic's current public naming uses
  `claude-opus-4-6`; `claude-opus-4.6` with a dot does not match the published
  model alias format. The existing template value `claude-sonnet-4-20250514`
  also looks stale relative to that.
- Agent CLI report submission currently:
  - reads role from env unless overridden
  - reads step from pipeline state unless overridden
  - accepts `--message-file`
  - accepts `--files-changed`
  - normalizes `fail` -> `reject`
  - writes `TaskThreadComment` through YAML-backed `append_thread_comment()`
- The current command surface is too permissive for an internal agent-only CLI.
  If agents are not supposed to override role/step/task semantics, the CLI
  should enforce the orchestrator-provided context rather than offer escape
  hatches.

Concrete actions:

- Redesign workspace config around a minimal operator-facing surface, not a dump
  of every internal default.
- Group engine-specific config under engine keys instead of flat
  `<engine>_model` fields, for example:
  - `engines.codex.model`
  - `engines.claude.model`
  - `engines.claude.max_turns`
- Review each current template field and classify it:
  - operator-facing and necessary
  - advanced but supported
  - internal/runtime detail that should not be in the template
- Remove unnecessary defaults from the template so omitted values cleanly fall
  back in code.
- Update Claude default model naming after confirming the desired Anthropic
  target:
  - if the team wants the latest rolling Opus line, use the current official
    alias format
  - do not use dotted model ids
- Add short rationale comments near `VERDICT_ALLOWLIST` explaining:
  - how agents submit verdicts
  - why each role's allowed verdicts differ
  - why planner/SWE cannot emit certain verdicts
- Remove agent CLI options that are not part of the intended agent contract:
  - `--message-file` if unused
  - `--role`
  - `--step`
  - possibly `--files-changed`
- Audit prompts/instructions and runtime code for any dependence on those
  options before removing them.
- Stop normalizing verdict aliases like `fail -> reject`. Accept only canonical
  verdict values and fail loudly on anything else.
- Search the codebase and prompt/instruction text for non-canonical verdict
  names and update them to the canonical set.
- Change agent report persistence to SQLite-backed storage rather than
  `comments.yaml`.
- Hide `SqlitePersistence(root)` and similar storage construction behind an
  injected repository/service where practical, especially in CLI entrypoints.
- Simplify sentinel-based update plumbing in `agent update` so the intent is
  explicit and readable, while preserving the distinction between "unset" and
  "set to empty".

## CLI package cleanup and command surface reduction

Question:

- rename `*_cli.py` files to plain names inside `litehive/cli/`
- remove `pipeline rules`
- remove all `v2` wording from codebase/help/docs
- improve command descriptions so help explains purpose, not just mechanics
- do not bypass persistence abstractions with raw SQL in CLI
- `SqliteJournal` + `SqlitePersistence` should likely collapse into one storage
  abstraction
- CLI may need class-based commands / dependency injection for storage mocking
- several CLI modules look redundant, fallback-only, overly abstract, or dead
- queue/pool/task/workspace/worktree command surfaces are repetitive and hard to
  understand

Current code shape:

- `litehive/cli/app.py` imports modules like `agent_cli.py`, `task_cli.py`,
  `queue_cli.py`, etc. The naming is redundant inside the `cli/` package.
- `register_hidden_root_commands()` in `queue_cli.py` injects top-level commands
  (`recover`, `prioritize`, `switch`) instead of exposing them through a normal
  typed command group.
- `daemon_cli.py` is a thin pass-through wrapper over `runner.py`.
- `dry_run.py` is used only by `runner.py`.
- `attention.py` appears unused in the codebase.
- `pipeline_cli.py` still exposes:
  - `rules`
  - `set-state`
  - `reset`
  - `journal`
- `pipeline reset` deletes rows with raw SQL directly instead of going through a
  storage abstraction.
- `pipeline journal` uses both `SqliteJournal` and `SqlitePersistence`, which
  splits one storage concern across two CLI-facing abstractions.
- `archive_cli.py` uses a custom `TyperGroup` plus Click internals to emulate
  "subcommand or implicit archive" behavior.
- `task_cli.py` still consults archive `INDEX.csv` even though task state now
  lives in SQLite.
- `task_debug_support.py` and `task_logs_support.py` are helper modules used
  only by `task_cli.py`.
- `worktree_support.py` contains substantial domain logic despite living under
  `cli/`.
- There are still many user-visible and internal `v2` references across help
  text, docstrings, docs, and tests.

Answer:

- Renaming `*_cli.py` files to plain names is sensible and improves local
  clarity.
- `pipeline rules` is display-only introspection with little operator value and
  should be removed if the transition table is not part of the intended public
  CLI.
- Command help text should explain:
  - what the command changes or inspects
  - why an operator would use it
  - when it is the right tool versus neighboring commands
- The split between `SqlitePersistence` and `SqliteJournal` is awkward at the
  CLI boundary. From the CLI's point of view, both are just pipeline runtime
  storage. A single storage/repository facade would be clearer.
- Yes, CLI can use classes and dependency injection. Typer commands can be thin
  wrappers around methods on injected services/controllers. That would make
  storage mocking and test setup cleaner.
- `daemon_cli.py` is redundant as a pass-through layer.
- `dry_run.py` is real code today, but if the product does not need dry-run
  planning, removing it simplifies both `runner.py` and the pool-reporting path.
- `cli/attention.py` appears dead and should be deleted unless something outside
  the repo invokes it.
- `archive_cli.py` is harder than it needs to be because of the custom group
  behavior. The Click/Typer hybrid is a smell, not a requirement.
- `cli/parse.py` is a mixed bag:
  - task-specific parsers belong closer to task/domain code
  - runner hook parsing belongs with config parsing
  - generic text-list parsing should not live in a CLI catch-all module if a
    domain module already owns normalization
- `pool.py` is highly verbose and heavily YAML/report-file dependent.
  If those statistics matter, they should come from durable runtime/report
  storage, not from ad hoc report-file scans.
- `queue_cli.py` overlaps multiple concepts:
  - inspect queue
  - move queued tasks
  - requeue/resume closed or interrupted tasks
  - switch engine
  The surface should be reduced and explained explicitly.
- `task_debug_support.py`, `task_logs_support.py`, and `worktree_support.py`
  should move out of `cli/` because they implement domain/support behavior, not
  command registration.
- `workspace.py` is currently a mixed command module that imports heavily from
  observability. The domain vocabulary is blurry and needs to be clarified.

Concrete actions:

- Rename CLI modules to drop the redundant `_cli` suffix:
  - `agent_cli.py` -> `agent.py`
  - `archive_cli.py` -> `archive.py`
  - `daemon_cli.py` -> `daemon.py`
  - `pipeline_cli.py` -> `pipeline.py`
  - `queue_cli.py` -> `queue.py`
  - `task_cli.py` -> `task.py`
  - `worktree_cli.py` -> `worktree.py`
- Update imports, tests, and docs after that rename.
- Remove user-facing `v2` wording everywhere:
  - CLI help strings
  - user-facing error messages
  - docs
  - prompt instructions
  - test names/docstrings where practical
- Keep migration-internal/database comments only where they describe historical
  schema facts rather than the public product surface.
- Add a command-help writing rule:
  - each command should have a short "what/why/when" description
  - avoid vague verbs like "dump"
  - prefer "show lifecycle history for a task so you can diagnose why it
    stopped" over "dump journal"
- Apply that help-text rule across all command registrations, especially:
  - pipeline commands
  - queue commands
  - runner/backup/db commands
  - workspace commands
- Remove `pipeline rules`.
- Keep `pipeline set-state` only if it is a supported operator escape hatch.
  If kept:
  - document why it exists
  - explain when to use it and the risks
  - surface that description in help text
- Replace raw SQL deletion in `pipeline reset` with a method on the pipeline
  storage abstraction.
- Collapse `SqlitePersistence` and `SqliteJournal` behind one higher-level
  pipeline runtime repository/service for CLI use. Candidate responsibilities:
  - load/save/reset task pipeline state
  - list lifecycle journal entries
  - list transitions
  - manage sessions
- Choose one name for that abstraction, such as:
  - `PipelineStore`
  - `PipelineRuntimeStore`
  - `PipelineRepository`
- Use one abstraction consistently from CLI commands instead of mixing storage
  layers ad hoc.
- Move CLI command implementations behind injectable controller/service classes
  where it simplifies testing, especially for:
  - pipeline commands
  - runner/backup/db commands
  - workspace commands
- Delete dead `cli/attention.py` unless a real call site is found.
- Remove `daemon_cli.py` and register daemon commands directly from the actual
  daemon command module.
- Remove `dry_run.py` and the `--dry-run` task-selection path if that workflow
  is not intentionally supported. If any dry-run capability remains, keep it
  near the command that owns it rather than in a generic side module.
- Simplify `archive_cli.py` by removing the custom `ArchiveGroup` / Click-Typer
  hybrid. Prefer explicit normal commands:
  - `archive task <task-id>`
  - `archive all-done`
  - `archive cleanup`
- Remove archive `INDEX.csv` usage from `task_cli.py` and any related code.
  Archived task lookup should come from current durable state, not CSV.
- Move task-specific parsers out of `cli/parse.py`:
  - dependency id parsing -> task/domain layer
  - acceptance criteria parsing -> task/domain layer
  - generic task text-list parsing -> task/domain layer
- Move runner-hook parsing out of `cli/parse.py` into config parsing/loading.
- Remove unused parser helpers from `cli/parse.py` after a call-site audit,
  including `parse_engine_int_map()` if nothing meaningful depends on it.
- Reduce queue command overlap:
  - define the exact purpose of `promote`, `requeue`, `resume`, `prioritize`,
    and `switch`
  - remove duplicate behaviors where one command can delegate to another
  - document the differences in help text
- Add queue/task status vocabulary to `docs/vocabulary.md`, including:
  - `interrupted`
  - `parked`
  - `flagged`
  - `cancelled`
  - `wont_do`
  - `deferred`
  - `duplicate`
- Revisit whether all of those task statuses are truly needed, or whether some
  can collapse into fewer clearer outcomes.
- Remove `register_hidden_root_commands()` and register commands in a normal,
  explicit Typer structure.
- Standardize one Typer command definition style across the CLI package.
- Remove `rollback` and top-level `report` commands from `runner.py` if they are
  no longer part of the supported surface.
- Move backup-related commands out of `runner.py` into a dedicated backup
  module, and database commands into a dedicated db module.
- Audit whether `backup create/list/restore` are actually used and document
  exactly what they back up. Current behavior is workspace runtime database
  backup, which should be made explicit in both naming and help text.
- Move `task_debug_support.py` and `task_logs_support.py` out of `cli/` into a
  non-CLI package, for example:
  - `litehive/debug/`
  - `litehive/inspection/`
  - `litehive/runtime_artifacts/`
- Audit how logs are stored today and document the storage model:
  - daemon run logs under workspace logs
  - task journal
  - subagent stdout/stderr/transcript artifacts
- Pick a package name for log/artifact inspection that does not conflict with
  Python `logging`, and move CLI helpers behind that package.
- Move `worktree_support.py` out of `cli/`; it is domain/worktree orchestration,
  not CLI glue.
- Reorganize `workspace.py` into smaller command-oriented modules or services,
  and document the vocabulary boundary between:
  - workspace management
  - observability/status
  - repair/doctor
  - engine control
