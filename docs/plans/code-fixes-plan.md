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
  `docs/domain.md`, including:
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
  - CLI `litehive report --stage`
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

- Revise `docs/domain.md` so every concept chooses exactly one canonical
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
- Standardize enum presentation in `docs/domain.md`:
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

These are now chosen in `docs/domain.md` and should be treated as the
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
- For each domain, keep three things together in the doc:
  - the purpose of the domain
  - the actors and actions that justify why the types exist
  - the target Python types for the entities in that domain
- Revisit the following still-open simplification questions in-place within
  their domains:
  - whether `PipelineStateView` should exist separately from `PipelineState`
  - whether `TaskStatus` should collapse further
  - whether `StagePhase` should exist at all
  - whether `RecoveryResult` is the final name

## Domain Simplification Decisions

These simplifications were chosen to make the target model easier to understand
without adding new behavior.

- Merge `RecoveryDiagnostics` into `FailureDiagnostics`.
  Reason: recovery and reporting were carrying near-duplicate structured
  failure data.
- Split `TaskRuntime` into `PipelineRuntime` and `ExecutionRuntime`, then keep
  `TaskRuntime` as the container holding those two slices.
  Reason: one runtime record had grown to mix pipeline position and retry state
  with subagent execution and resumability details.
- Remove the dedicated `SessionToken` wrapper type and use a plain string for
  engine continuation tokens.
  Reason: a one-field wrapper added vocabulary without adding domain meaning.
- Remove `files_changed` from the target `StageReport`.
  Reason: changed files are derivable from git and do not need to be part of
  the core report model.

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
  update report resolution to read from SQLite instead of
  `comments.yaml`.
- Add `docs/domain.md` defining the canonical terms for:
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
- report path should not append YAML comments
- some option/default plumbing is hard to read

Answer:

- The workspace template currently exposes many defaults and implementation
  details. It reads more like an exhaustive config dump than a curated operator
  surface.
- As of April 15, 2026, Anthropic's current public naming uses
  `claude-opus-4-6`; `claude-opus-4.6` with a dot does not match the published
  model alias format. The existing template value `claude-sonnet-4-20250514`
  also looks stale relative to that.
- Report submission CLI currently:
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
  - `--stage`
  - possibly `--files-changed`
- Audit prompts/instructions and runtime code for any dependence on those
  options before removing them.
- Stop normalizing verdict aliases like `fail -> reject`. Accept only canonical
  verdict values and fail loudly on anything else.
- Search the codebase and prompt/instruction text for non-canonical verdict
  names and update them to the canonical set.
- Change report persistence to SQLite-backed storage rather than
  `comments.yaml`.
- Hide `SqlitePersistence(root)` and similar storage construction behind an
  injected repository/service where practical, especially in CLI entrypoints.
- Simplify sentinel-based update plumbing in `litehive task update` so the intent is
  explicit and readable, while preserving the distinction between "unset" and
  "set to empty".

## CLI package cleanup and command surface reduction

## Deletion follow-up after the first recovery/domain pass

Question:

- The first implementation pass introduced the new recovery/domain types, but
  it intentionally kept several legacy fields and compatibility accessors so
  the codebase would keep running during the migration.
- What is still left to delete, and which behavior should disappear with those
  deletions instead of being preserved forever?

Answer:

- The remaining cleanup is no longer mostly about naming. It is about removing
  transition scaffolding and breaking apart a few remaining junk-drawer models.
- The main leftovers fall into four buckets:
  - lifecycle recovery state still keeps old transitional fields that duplicate
    the new structured recovery objects
  - runtime/report models still expose legacy `step` / `stage` / `trigger`
    compatibility shims instead of one canonical shape
  - generic context payloads are still doing too many jobs at once
  - some old persistence/reporting paths are still present, so the surrounding
    helper code cannot be deleted yet

What is still left to delete:

- Delete transitional recovery counters/markers that are now redundant with the
  structured model:
  - `TaskState.recovery_attempt`
  - `StateDelta.inc_recovery_attempt`
  - CLI/prompt/report code that prints or depends on the per-stage
    `recovery_attempt` dict instead of deriving history from
    `RecoveryOutcome`
- Delete `TaskState.origin_stage` once resume logic reads the origin from the
  active structured trigger or the persisted recovery outcome:
  - `StateDelta.set_origin_stage`
  - `StateDelta.clear_origin_stage`
  - prompt/CLI plumbing that separately renders `origin_stage`
- Delete `TaskState.pre_exec_recovery_attempt` and the related "counter"
  behavior if pre-exec recovery is truly one-shot:
  - replace it with a dedicated boolean/enum or a structured pre-exec recovery
    record
  - then remove the counter increment/check logic from:
    `guards.py`, `nodes/system.py`, persistence payloads, and related tests
- Delete generic `failure_context` after splitting it into domain-specific
  payloads:
  - `recovery_context` or direct use of `RecoveryTrigger` /
    `RecoveryOutcome`
  - merge-conflict-specific context
  - commit-result-specific context
  - remove code that treats one free-form dict as the carrier for all of those
    unrelated concerns
- Delete `thread` piggybacking inside lifecycle recovery context:
  - `RoleAgent.build_prompt()` currently reads `state.failure_context["thread"]`
  - thread/discussion state should come from the discussion store, not from a
    generic recovery payload

Compatibility fields/accessors left to delete:

- Delete `step` compatibility properties/aliases after call sites migrate to
  `stage`:
  - `RuntimeStageState.step`
  - `RuntimeContinuationHandoff.step`
  - `RuntimeEngineSwitch.step`
  - `StageReport.step`
  - `TaskThreadComment.step`
- Delete `RecoveryReport.stage` and `RecoveryReport.trigger` compatibility
  aliases after all callers use:
  - `origin_stage`
  - `trigger_event_kind`
- Delete CLI/reporting surfaces that still speak in `step` terms:
  - `litehive report --stage`
  - CLI output lines such as `step: ...`
  - prompt-serializer thread rendering that still emits `step`
- Delete verdict alias normalization that only exists for backward
  compatibility:
  - `accept -> pass`
  - `fail -> reject`
  - agents/CLI/tests should submit canonical verdicts only
- Delete type-alias bridges once the canonical enum names are chosen:
  - `PipelineState = PipelineStatus`
  - `RunnerExecutionStatus = RunnerStatus`

Oversized models that still need to be broken down before more deletion:

- `TaskRuntime` is still carrying multiple concerns:
  - run activity state
  - current/last stage runtime state
  - interruption state
  - continuation handoff state
  - engine switch state
  - hook-reject loop bookkeeping
  - last outcome state
- `TaskState.failure_context` is still a junk drawer for:
  - recovery trigger data
  - merge conflict files / merge attempt count
  - commit result data
  - ad hoc prompt context
- `tasks/runtime.py` still contains broad helper functions whose signatures and
  internal branching are shaped around those large models; once the models are
  split, some of those helpers should disappear rather than be mechanically
  updated

Concrete actions:

- Migrate recovery resume/terminal logic to use only:
  - `active_recovery_trigger`
  - `recovery_history`
  - `recovery_failure_explanation`
  - then delete `recovery_attempt`, `origin_stage`, and their delta fields
- Introduce a dedicated structured pre-exec recovery state and then delete
  `pre_exec_recovery_attempt`
- Split `failure_context` into explicit domain objects/fields and then delete
  the generic dict:
  - recovery-owned context
  - merge-owned context
  - commit-result field or record
- Remove all `step` compatibility properties and alias-accepting validation
  only after migrating:
  - runtime helpers
  - CLI flags/output
  - prompt serializer
  - tests and fixtures
- Tighten `RecoveryReport` so `trigger_event_kind` uses the actual enum type
  and delete the legacy `trigger`/`stage` property shims
- Remove verdict alias normalization and update every caller/test/prompt to the
  canonical verdict vocabulary
- Choose one canonical enum name for pipeline/runner state and delete the alias
  re-exports from `domain/common.py`
- Migrate task discussion/verdict persistence off `comments.yaml`, then delete:
  - YAML comment helpers
  - YAML-thread compatibility logic in prompt assembly/report resolution
  - field/functionality that only exists because comment storage is file-backed

Validation steps:

- Add tests that fail if old payload keys are still written for new rows:
  - `recovery_attempt`
  - `origin_stage`
  - `failure_context`
  - `step`
  - `trigger`
- Add CLI tests that assert canonical output/flags only:
  - `stage`, not `step`
  - `trigger_event_kind`, not `trigger`
  - canonical verdicts only
- Add persistence round-trip tests that prove the smaller replacement records
  are sufficient before removing the old fields.

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
- Add queue/task status vocabulary to `docs/domain.md`, including:
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
  - background-run logs under workspace logs
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

## Codebase simplification plan from actual code read

This section supersedes the older historical package-structure notes. It is
based on reading the current codebase on 2026-04-15, not on an older
pre-rename layout.

### Analysis snapshot

What the current codebase actually looks like:

- Litehive still persists a large amount of structured state as YAML:
  - `.litehive/config.yaml`
  - task `task.yaml`
  - `comments.yaml`
  - stage/recovery `*.yaml` reports
  - subagent `session.yaml` / `report.yaml` / `timeline.yaml`
  - `engine-monitoring.yaml`
  - pool-run summary YAMLs
  - runner/daemon lock metadata YAML
- `tasks/` and `state/` are the tightest knot in the tree.
  Import scan:
  - `tasks/` imports `state/` 67 times
  - `state/` imports `tasks/` 8 times
  - that is a strong signal that the current package boundary is wrong
- pipeline state is split across four SQLite-facing abstractions:
  - `litehive/state/store.py` -> workspace/task runtime rows
  - `litehive/lifecycle/persistence.py` -> pipeline state row
  - `litehive/lifecycle/journal.py` -> pipeline journal + transitions
  - `litehive/lifecycle/sessions.py` -> pipeline session rows
- queue state is already partly in SQLite, but task identity/content is still
  split against file-backed task records
- task activity and verdict submission are still filesystem-era:
  - `litehive/tasks/reports.py` persists `comments.yaml`
  - `litehive/agents/parsing.py` reads that YAML back to synthesize a
    `StageReport`
  - runtime decisions still depend on that file path
- package names no longer match responsibilities:
  - `lifecycle/` is the pipeline runtime
  - `agents/` is mostly execution/runtime orchestration, not domain agents
  - `observability/` mixes rendering, diagnostics, JSONL event writing, and
    engine monitoring
  - `state/` mixes locks, repositories, write orchestration, and backups
  - `tasks/` mixes queue logic, task mutations, activity storage, worktrees,
    normalization, and archive handling
- the CLI surface grew by accretion:
  - several commands overlap (`promote` / `requeue` / `resume` /
    `prioritize`)
  - some command modules are wrappers over other command modules
  - some support files are presentation-only but live as top-level helpers
  - some debug/report/dry-run paths are likely maintenance leftovers rather
    than part of the intended operator surface

### Target package direction

Recommended target package layout:

```text
litehive/
  domain/        # pure target models, enums, value objects
  pipeline/      # state machine, nodes, transitions, pipeline runtime store
  execution/     # subagent execution, engine runtime orchestration
  task/          # task repository, queue, mutation services, archive
  activity/      # task activity entries, stage verdict submission, rendering
  workspace/     # locks, daemon loop, worktrees, repair, health probes
  storage/       # sqlite connection + migrations + backup utilities
  config/        # operator-facing config only
  roles/         # role-specific prompt/verdict behavior
  sandbox/       # sandbox infrastructure
  cli/           # thin command presentation only
  git/           # git operations
```

Notes:

- keep `domain/` as the pure model layer
- use `pipeline/` consistently instead of `lifecycle/`
- remove `observability/` as an umbrella package; split it by actual domain
- remove `state/` as an umbrella package; split it by actual ownership
- keep `config/` limited to config concerns; move runtime policy out of it
- storage target: SQLite only for Litehive-owned structured data
  - the only YAML file that remains is `.litehive/config.yaml`
  - no task YAML
  - no comments/report/session/timeline YAML
  - no engine-monitoring YAML
  - no pool summary YAML
  - queue stays in SQLite
  - incomplete tasks move fully to SQLite
  - text logs/transcripts may remain plain text if unstructured
  - every other Litehive-owned structured file format should be removed

## Merge plan

### 1. Merge `TaskRecord`, `TaskIntentRecord`, and `TaskStateRecord` into one domain task model plus storage adapters

Current code:

- `litehive/domain/task.py` defines:
  - `TaskIntentRecord`
  - `TaskStateRecord`
  - `TaskRecord`
- it also defines three git settings types:
  - `GitSettings`
  - `TaskIntentGitSettings`
  - `TaskStateGitSettings`

Why this should be merged:

- the domain has one task, not three task-shaped domain objects
- the split is storage-driven, not domain-driven
- the split is also still YAML-era: task intent is persisted in `task.yaml`
- queue order already lives in SQLite, so task content being file-backed creates
  a split-brain persistence model for incomplete work
- code has to keep converting back and forth:
  - `TaskRecord.to_intent_record()`
  - `TaskRecord.to_state_record()`
  - `TaskRecord.to_storage_state_record()`
  - `TaskRecord.from_intent_and_state(...)`
- the same duplication exists for git fields

Target:

- keep one domain model:
  - `Task`
- move storage shaping to SQLite row adapters/repository code
- keep one task-owned git model:
  - `TaskGit`

Concrete implementation steps:

- define the desired `Task` shape first in code, matching `docs/domain.md`
- move intent/state split logic out of `domain/task.py`
- add a SQLite-backed task repository for both task intent and runtime/state
- migrate all incomplete tasks to SQLite-backed task rows:
  - queued
  - in_progress
  - interrupted
  - parked
  - flagged
  - merge_failed
  - cancelled
  - wont_do
  - deferred
  - duplicate
- stop writing `task.yaml`
- remove `TaskIntentRecord`, `TaskStateRecord`,
  `TaskIntentGitSettings`, and `TaskStateGitSettings` after all storage call
  sites are migrated

Validation:

- task create/load/save round-trips still preserve current task data
- no caller outside the repository layer needs to know how task state is split
  in SQLite
- queue selection works entirely from SQLite-backed task + queue state without
  consulting task files

### 2. Merge `state/records.py`, `state/persist.py`, `state/store.py`, and task runtime write helpers into repository/services by domain

Current code:

- `litehive/state/records.py` owns task YAML I/O and SQLite task-state I/O
- `litehive/state/locking.py` persists runner metadata as YAML
- `litehive/state/persist.py` owns atomic write orchestration and mixed
  file+SQLite transactions
- `litehive/state/store.py` owns low-level SQLite runtime store methods
- `litehive/tasks/runtime.py` owns task runtime mutation helpers that persist
  through the above layers

Why this should be merged:

- the current split is implementation-detail-driven, not domain-driven
- callers have to know too much about which helper lives in which file
- task mutation code crosses packages constantly:
  - mutate a task model in `tasks/runtime.py`
  - write runtime rows through `state/store.py`
  - load/save task files through `state/records.py`
- the code is still paying complexity for a mixed YAML+SQLite world we do not
  want to keep
- incomplete-task lifecycle operations are forced through both queue rows and
  task files today

Target:

- `task/repository.py`
  - load/save tasks
  - persist task intent/state split internally
- `task/runtime_repository.py` or repository methods on `TaskRepository`
  - save runtime slices
- `task/service.py`
  - task mutations such as close/requeue/resume/update
- `storage/sqlite.py`
  - raw SQLite connection helpers only

Concrete implementation steps:

- move `records.py` responsibilities into a task repository
- move `persist.py` write orchestration into SQLite transaction/repository code
- shrink `RuntimeStore` so it is either:
  - absorbed by task/pipeline/activity repositories
  - or kept as a thin internal storage adapter, not exposed widely
- move `tasks/runtime.py` write helpers behind repository/service methods so
  callers stop mutating and persisting through separate helper layers
- replace YAML lock metadata with either:
  - SQLite-backed runner/daemon status rows
  - or plain lockfiles with no YAML payload if only process exclusion is needed
- make queue mutation + incomplete-task mutation one SQLite transaction where
  they currently have to coordinate file and DB writes

Validation:

- task mutations become SQLite-only and stay atomic within one store
- callers no longer import from both `state.*` and `tasks.*` just to update one
  task
- requeue/resume/stop/close/switch do not touch `task.yaml`

### 3. Merge pipeline SQLite abstractions behind one pipeline storage boundary

Current code:

- `litehive/lifecycle/persistence.py`
  - `SqlitePersistence`
- `litehive/lifecycle/journal.py`
  - `SqliteJournal`
- `litehive/lifecycle/sessions.py`
  - `SqliteSessionStore`

Why this should be merged:

- these are all one concern from the pipeline package's point of view:
  pipeline runtime storage
- CLI and orchestration code currently need to assemble several storage objects
  for one conceptual subsystem
- `pipeline reset` bypasses them with raw SQL, which is a direct symptom of the
  abstraction split being wrong

Target:

- `pipeline/store.py` or `storage/pipeline_store.py` with sub-APIs for:
  - state
  - journal
  - sessions
- keep separate internal tables, but expose one pipeline-owned storage facade

Concrete implementation steps:

- define a single pipeline storage owner used by:
  - orchestration
  - pipeline CLI
  - tests
- move `reset()` / `load_journal()` / `load_transitions()` / session methods
  under that facade
- delete direct CLI SQL deletion in `pipeline reset`

Validation:

- existing pipeline CLI behavior remains possible without raw SQL in CLI code
- orchestration depends on one injected pipeline store instead of three sqlite
  wrappers

### 4. Merge task activity, stage verdict submission, and recovery notes into one activity model/store

Current code:

- `litehive/domain/reports.py`
  - `TaskThreadComment`
  - `StageReport`
  - `RecoveryReport`
- `litehive/tasks/reports.py`
  - appends and reads `comments.yaml`
  - writes recovery reports as YAML
  - renders task thread
- `litehive/agents/parsing.py`
  - reads YAML comments to synthesize a `StageReport`
- `litehive/cli/agent_cli.py` and `litehive/cli/runner.py`
  - both submit report/thread-style entries

Why this should be merged:

- verdicts, comments, and operator/recovery notes are one append-only activity
  stream conceptually
- right now the activity model is split between:
  - YAML `comments.yaml`
  - `StageReport`
  - free-form recovery note insertion
- subagent/session/report YAML artifacts are also feeding recovery/debug flows
- `StageReport` still carries filesystem-era fields like `files_changed`

Target:

- `activity/`
  - `models.py`
  - `store.py`
  - `rendering.py`
- one append-only activity entry model with optional structured fields for:
  - verdict
  - pipeline state
  - author
  - message
- separate stage/run reports only if they add real data beyond the entry itself

Concrete implementation steps:

- first move YAML-thread semantics behind an activity store interface
- then add SQLite-backed activity persistence
- add SQLite-backed stage-run, recovery-record, and subagent-session tables
- then update:
  - `agent_cli`
  - prompt serializer
  - recovery reporting
  - stage report resolution
  to read/write through the activity store
- then delete filesystem `comments.yaml` support
- then delete YAML `report/session/timeline/recovery` artifacts that only store
  structured data
- remove `files_changed` from target report/activity structures

Validation:

- prompt serialization still sees recent verdict context
- recovery notes and agent verdicts still show up in task history
- no runtime decision depends on parsing YAML artifacts after the migration

### 5. Merge queue selection and task status transitions under one task application layer

Current code:

- `litehive/tasks/queue.py`
  - queue mutations
  - task selection
  - auto-recovery staging
- `litehive/tasks/status.py`
  - stop
  - switch
  - requeue
  - resume
  - close
  - park
  - update
- CLI command overlap mirrors this split

Why this should be merged carefully:

- queue selection and task mutation are different behaviors, so they do not
  belong in one file
- but they do belong under one task-owned application layer with shared
  repository/service dependencies
- today they are heavily coupled through inline imports and circular calls

Target:

- `task/queue_service.py`
  - ordering and selection
- `task/service.py`
  - state transitions and operator actions
- shared repository dependencies injected explicitly

Concrete implementation steps:

- keep selection logic separate from status transitions
- remove inline cross-imports between queue and status
- introduce one task application layer used by CLI and daemon code
- collapse overlapping command behaviors so one command implementation can call
  another service method instead of duplicating logic
- make queue operations and incomplete-task state transitions operate on one
  SQLite-backed task store instead of queue rows plus file-backed task data

Validation:

- queue ordering rules remain unchanged
- task mutation commands still preserve current state transitions until the
  domain vocabulary cleanup lands

### 6. Merge worktree management logic into one workspace-owned area

Current code:

- `litehive/tasks/worktrees.py`
- `litehive/cli/worktree_support.py`
- worktree-related repair logic also appears in:
  - `litehive/recovery/workspace_repair.py`
  - `litehive/lifecycle/orchestration.py`

Why this should be merged:

- worktrees are a workspace/runtime concern, not a CLI concern
- current logic is split across task, recovery, pipeline, and CLI support files
- the CLI support file contains substantial business logic and should not
  remain under `cli/`

Target:

- `workspace/worktrees.py`
  - inspection and ownership rules
- `workspace/worktree_rescue.py`
  - rescue/finalization logic
- CLI becomes a thin wrapper over those services

Concrete implementation steps:

- move `cli/worktree_support.py` out of `cli/`
- consolidate worktree rescue and inspection logic in one workspace package
- keep pipeline orchestration on a small interface such as:
  - resolve task worktree
  - clear stale worktree
  - finalize worktree after terminal states

Validation:

- worktree rescue, clean, and lifecycle cleanup behavior stay intact
- no domain logic remains inside CLI support files

### 7. Merge execution-session helpers into explicit execution collaborators

Current code:

- `litehive/agents/manager.py`
- `litehive/agents/session.py`
- `litehive/agents/artifacts.py`
- `litehive/agents/parsing.py`

Why this should be merged:

- `SubagentManager` currently owns too many responsibilities
- `SessionMixin` is single-consumer indirection
- artifact/session/report handling is spread across several small modules that
  only make sense together

Target:

- `execution/manager.py`
- `execution/session_writer.py`
- `execution/result_classifier.py`
- optional `execution/artifact_store.py`

Concrete implementation steps:

- merge `SessionMixin` into either:
  - the manager
  - or a concrete session writer collaborator
- keep artifact writing separate only if it becomes a reusable concrete store
- split result classification out of `SubagentManager.run()`
- move stage report resolution to activity/report code, not execution code

Validation:

- manager becomes shorter and easier to reason about without changing behavior
- execution start/progress/finish artifacts stay stable across the refactor

## Rename and package-structure plan

### 1. Rename `lifecycle/` to `pipeline/`

Current package:

- `litehive/lifecycle/`

Why:

- the user-facing and target domain terminology now prefers `pipeline`
- current docs/domain vocabulary already use `pipeline` consistently
- the package owns:
  - pipeline states
  - pipeline events
  - pipeline runner
  - pipeline journal

Mapping:

- `litehive/lifecycle/events.py` -> `litehive/pipeline/events.py`
- `litehive/lifecycle/runner.py` -> `litehive/pipeline/runner.py`
- `litehive/lifecycle/persistence.py` -> `litehive/pipeline/store.py` or
  `litehive/pipeline/state_store.py`
- `litehive/lifecycle/journal.py` -> `litehive/pipeline/store.py` or
  `litehive/pipeline/journal_store.py`
- `litehive/lifecycle/sessions.py` -> `litehive/pipeline/store.py` or
  `litehive/pipeline/session_store.py`
- `litehive/lifecycle/orchestration.py` -> `litehive/pipeline/run.py`
- `litehive/lifecycle/prompt_serializer.py` -> `litehive/pipeline/prompts.py`
- `litehive/lifecycle/nodes/` -> `litehive/pipeline/nodes/`

Notes:

- do this after the storage facade exists so the rename does not preserve bad
  abstractions under a better package name

### 2. Rename `agents/` to `execution/` and move generic Heru glue out of Litehive

Current package:

- `litehive/agents/`

Why:

- the package mostly runs external engines and manages subagent execution
- it is not the home of domain agents; role-specific behavior is already in
  `roles/`
- some of its code is actually Heru-owned generic runtime parsing

Mapping:

- `agents/manager.py` -> `execution/manager.py`
- `agents/session.py` -> `execution/session_writer.py`
- `agents/artifacts.py` -> `execution/artifact_store.py` if retained
- `agents/parsing.py` -> `activity/stage_reports.py` or
  `activity/report_resolution.py`
- `agents/sandbox.py`
  - split and move infra pieces to `sandbox/`
  - keep only execution-specific orchestration in `execution/`
- remove from Litehive entirely:
  - `agents/unified_events.py`
  - `agents/_continuation.py`

### 3. Split `tasks/` into `task/`, `activity/`, and `workspace/`

Current package:

- `litehive/tasks/`

Why:

- `tasks/` currently contains at least four different domains:
  - task repository and mutations
  - queue selection
  - activity/report storage
  - worktree handling

Mapping:

- keep under `task/`:
  - `tasks/queue.py` -> `task/queue_service.py`
  - `tasks/status.py` -> `task/service.py`
  - `tasks/normalization.py` -> `task/normalization.py`
  - `tasks/archive.py` -> `task/archive.py`
  - `tasks/constants.py` -> `task/constants.py`
- move to `activity/`:
  - `tasks/reports.py` -> split into `activity/store.py` and
    `recovery/reports.py`
  - `tasks/journal.py` -> decide whether it is task activity or remove it if
    superseded by activity entries
- move to `workspace/`:
  - `tasks/worktrees.py` -> `workspace/worktrees.py`
- reduce or remove:
  - `tasks/paths.py`
  - split path helpers by owner instead of one catch-all path module
- move runtime update helpers:
  - `tasks/runtime.py` -> `task/runtime_updates.py` or absorb into task service

### 4. Split `state/` into `storage/`, `workspace/`, and task-owned repositories

Current package:

- `litehive/state/`

Why:

- `state/` is not one domain
- it currently holds:
  - locks
  - backups
  - repositories
  - write orchestration
  - runtime-store glue

Mapping:

- `state/locking.py` -> `workspace/locks.py`
- `state/backup.py` -> `storage/backups.py`
- `state/store.py` -> absorb into:
  - `task/repository.py`
  - `pipeline/store.py`
  - `activity/store.py`
- `state/persist.py` -> repository transaction/orchestration code
- `state/records.py` -> `task/repository.py`

### 5. Remove `observability/` as an umbrella and rename by actual purpose

Current package:

- `litehive/observability/`

Why:

- it mixes three unrelated concerns:
  - rendering status text
  - diagnostics/doctor probes
  - event/log file writing
  - engine usage monitoring

Mapping:

- `observability/status.py` -> `cli/status_view.py`
- `observability/status_diagnostics.py` -> `workspace/diagnostics.py`
- `observability/engine_monitoring.py` -> `execution/engine_monitoring.py`
- `observability/events.py` -> split between:
  - `activity/events.py`
  - `execution/session_logs.py`
  depending on which records survive

### 6. Rename CLI modules to plain names inside `cli/`

Mapping:

- `cli/agent_cli.py` -> `cli/agent.py`
- `cli/archive_cli.py` -> `cli/archive.py`
- `cli/daemon_cli.py` -> remove, not rename
- `cli/pipeline_cli.py` -> `cli/pipeline.py`
- `cli/queue_cli.py` -> `cli/queue.py`
- `cli/task_cli.py` -> `cli/task.py`
- `cli/worktree_cli.py` -> `cli/worktree.py`

Additional CLI moves:

- `cli/worktree_support.py` -> `workspace/worktree_rescue.py`
- `cli/task_debug_support.py` -> `debug/tasks.py` if kept
- `cli/task_logs_support.py` -> `execution/logs.py` or `workspace/logs.py`
  depending on final ownership

### 7. Move runtime policy out of `config/engine_models.py`

Mapping:

- keep config-only parsing and defaults under `config/`
- move engine selection/runtime policy to:
  - `execution/engine_selection.py`
  - `execution/model_resolution.py`
  - `execution/engine_freezes.py` or `workspace/engine_freezes.py`
- move per-engine quota blocking to engine-owned adapters or a registry

## Removal plan

### 1. Remove modules that are wrapper-only, dead, or legacy-shape compatibility

Remove after call sites are migrated:

- `litehive/agents/_continuation.py`
- `litehive/agents/unified_events.py`
- `litehive/agents/prompts.py`
- `litehive/agents/session.py`
- `litehive/cli/daemon_cli.py`
- `litehive/cli/dry_run.py`
- `litehive/cli/parse.py`

Conditional removal or relocation:

- `litehive/cli/task_debug_support.py`
- `litehive/cli/task_logs_support.py`
- `litehive/observability/events.py`
- `litehive/tasks/journal.py`

### 2. Remove filesystem-era activity storage after SQLite activity store lands

Remove:

- `comments.yaml` reads/writes
- `task.yaml` reads/writes
- `engine-monitoring.yaml` reads/writes
- `session.yaml`
- `report.yaml`
- `timeline.yaml`
- stage/recovery `*.yaml` report files
- pool-run summary YAML files
- any code path that loads incomplete tasks from filesystem task records
- any structured YAML file other than `.litehive/config.yaml`
- `TaskThreadComment.files_changed`
- `normalized_files_changed(...)`
- pass-comment retraction logic that depends on claimed changed-file lists
- any report resolution path that parses YAML comments to infer verdicts

### 3. Remove redundant commands and hidden command registration

Remove:

- `litehive pipeline rules`
- hidden root command registration via `register_hidden_root_commands(...)`
- duplicate daemon command surfaces once one command path remains
- root-level operator `report` command in `cli/runner.py` if `cli/agent.py`
  becomes the single verdict submission path
- rollback command if rollback is no longer part of the supported operator
  workflow

Collapse rather than keep separate:

- `promote`, `resume`, `requeue`, and `prioritize`
- they can remain as CLI aliases if desired, but should route through one
  implementation and one shared state model

### 4. Remove stale archive/index and redundant artifact compatibility

Remove:

- archive `INDEX.csv` support in `tasks/archive.py` and `cli/task*.py`
- `prune_superseded_subagent_artifacts(...)`
- old filename compatibility like `stdout.log` / `stderr.log`
- YAML-based daemon/runner metadata payloads

### 5. Remove policy and naming leftovers that keep the old mental model alive

Remove:

- all user-facing `v2` wording in:
  - CLI help
  - recovery prompts
  - docs
  - tests where wording is part of expected output
- `__all__` declarations across Litehive
- verdict normalization aliases such as `fail -> reject`
- inline imports that are only compensating for package coupling instead of
  real optional/cycle boundaries

## Current implementation status

This section tracks what from the refactor has already landed and what still
remains to align the code with the storage target stated above.

### Done

- Recovery/state vocabulary cleanup:
  - compatibility `step` naming was removed from active code paths in favor of
    canonical `stage`
  - old recovery bookkeeping fields and related fallback behavior were removed
  - runtime/recovery state now uses the smaller structured recovery model
- Migration reset:
  - old multi-step schema history was collapsed into one baseline migration
  - legacy workspace databases are rebuilt from the new baseline
  - SQLite bootstrap repopulates current task rows from on-disk task files
- No-compat refactor pass:
  - old recovery/report compatibility aliases and fallback paths were removed
  - active CLI/prompt/report surfaces now use the new canonical field names
- Structured subagent artifact cleanup:
  - active subagent `session` / `report` / `timeline` storage no longer uses
    `session.yaml` / `report.yaml` / `timeline.yaml`
  - active execution, recovery, debug, and logs paths now read those
    structured artifacts from SQLite-backed subagent-session storage
- Text transcript/log artifacts are still allowed by plan and remain on disk:
  - plain-text transcript/log files are not part of the YAML removal target

### Still to do

- Remove filesystem-backed activity/report YAML:
  - delete `comments.yaml`
  - delete stage/recovery `*.yaml` report files
  - delete any report-resolution path that infers verdicts from YAML files
- Remove workspace-level structured YAML outside config:
  - delete `engine-monitoring.yaml`
  - delete pool-run summary YAML files
- Finish moving task durability off filesystem records:
  - stop using `task.yaml` as active Litehive-owned structured state
  - remove code paths that load incomplete tasks from filesystem task records
- Delete the remaining YAML-oriented helper and compatibility code once the new
  persistence paths land:
  - CLI/debug/log fallbacks that parse `session.yaml` / `report.yaml`
  - recovery helpers that read/write subagent YAML artifacts
  - artifact inventory/reporting code that still advertises YAML paths

### Remaining hotspots in code

- Subagent artifact writing:
  - `litehive/agents/session.py`
  - follow-up cleanup of old artifact inventory and pruning assumptions in
    `litehive/tasks/reports.py` and `litehive/agents/artifacts.py`
- YAML-backed activity/reporting:
  - `litehive/tasks/reports.py`
  - `litehive/lifecycle/prompt_serializer.py`
- YAML-backed monitoring/status:
  - `litehive/observability/engine_monitoring.py`
  - `litehive/observability/status.py`
  - `litehive/observability/status_diagnostics.py`
- Filesystem task persistence still in the active path:
  - `litehive/state/records.py`
  - `litehive/state/store.py`
  - `litehive/tasks/archive.py`
  - `litehive/attention.py`

### Validation target for completion

- `.litehive/config.yaml` is the only remaining YAML file owned by Litehive
- no `comments.yaml`
- no `session.yaml`
- no `report.yaml`
- no `timeline.yaml`
- no `engine-monitoring.yaml`
- no stage/recovery/pool summary YAML
- no runtime decision depends on parsing structured YAML artifacts

### Inventory snapshot of current code

This is the current-state inventory from the codebase, not the target state.
It is intentionally specific so refactoring tasks can be scoped against the
real implementation rather than against assumptions.

#### Already landed

- Recovery/state cleanup already landed:
  - active code uses `stage` instead of the old `step` compatibility naming
  - old recovery bookkeeping fields/fallbacks were removed
- Baseline schema reset already landed:
  - one baseline migration is in place
  - old workspace databases are rebuilt from that baseline
- Structured subagent YAML removal is partially complete and active:
  - active subagent session/report/timeline data no longer uses
    `session.yaml` / `report.yaml` / `timeline.yaml`
  - subagent structured artifacts now flow through SQLite-backed session
    storage
  - plain-text logs/transcripts still remain on disk by design

#### Still active in code today

- `task.yaml` is still active runtime storage, not just import/bootstrap input:
  - `litehive/state/records.py`
  - `litehive/state/store.py`
  - `litehive/attention.py`
  - `litehive/tasks/archive.py`
  - corresponding tests and docs still assert that incomplete-task state lives
    in `task.yaml`

- Activity storage is only partially migrated:
  - `task_activity` exists in the schema
  - `litehive/tasks/reports.py` has started moving thread persistence to SQLite
  - but the code still exposes old thread/comment naming:
    - `TaskThreadComment`
    - `append_thread_comment`
    - `load_task_thread`
    - `save_task_thread`
  - many call sites, tests, and prompt/CLI strings still use “discussion
    thread” terminology

- Structured YAML report storage is still active:
  - `litehive/tasks/reports.py` still writes recovery reports as
    `recovery-*.yaml`
  - recovery prompts still point operators/agents at `reports/*.yaml`
  - the current activity/report code still synthesizes report context from the
    filesystem-era layout

- Workspace-level monitoring YAML is still active:
  - `litehive/observability/engine_monitoring.py`
  - `litehive/observability/status_diagnostics.py`
  - workspace bootstrap/gitignore still mention `engine-monitoring.yaml`

- Task status semantics still diverge from the target domain:
  - `litehive/domain/common.py` still defines:
    - `merge_failed`
    - `cancelled`
    - `wont_do`
    - `deferred`
    - `duplicate`
  - `litehive/tasks/status.py` and `litehive/lifecycle/orchestration.py` still
    persist those values directly

- Pipeline vocabulary is still split:
  - `PipelineState = PipelineStatus` alias still exists in
    `litehive/domain/common.py`
  - actual machine phase is still represented elsewhere:
    - `LifecyclePhase`
    - `TaskState.stage`
    - `TaskRuntime.current_stage.stage`
  - prompts and runtime surfaces still consume raw stage strings

- Runtime structure is still flat:
  - `litehive/domain/runtime.py` still has one `TaskRuntime` carrying:
    - pipeline progression
    - execution/subagent state
    - git state
    - hook-reject recovery state
    - outcome state
  - code does not yet use the target `TaskRuntime.pipeline` /
    `TaskRuntime.execution` split from `docs/domain.md`

- Recovery naming still reflects the current implementation model, not the
  target document model:
  - `FailureFingerprint`
  - `RecoveryTrigger`
  - `RecoveryOutcome`
  - `RecoveryDisposition`
  - these are active in persistence, prompts, and transition logic today

## Gradual factoring task list

This is the recommended implementation queue for Litehive-managed work. The
goal is to factor the refactor into independently shippable tasks with clear
validation boundaries, rather than mixing naming, storage, and behavior
changes in one pass.

The task-by-task queue mirror lives in `docs/refactoring-tasks.md`.

### Track A. Activity and report cleanup

1. Introduce an activity boundary without changing behavior.
   Scope:
   - add an activity-oriented service/store boundary
   - move existing thread/comment read-write calls behind that boundary
   - keep current payload shapes temporarily
   Why first:
   - this gives later renames and storage changes one seam to migrate through
   Validation:
   - prompt serialization, report submission, and task debug output are
     unchanged

2. Rename thread/comment vocabulary to activity vocabulary.
   Scope:
   - replace `TaskThreadComment` with `ActivityEntry`
   - replace `load_task_thread` / `save_task_thread` / `append_thread_comment`
     with activity-oriented names
   - replace user-facing "discussion thread" naming where it really means
     task activity
   Validation:
   - no active code references `TaskThreadComment`
   - CLI and prompts still show recent human-readable history

3. Move task activity persistence to SQLite and delete `comments.yaml`.
   Scope:
   - back activity entries with the SQLite `task_activity` table
   - remove filesystem `comments.yaml` support and corrupt-YAML fallbacks
   - update prompt/recovery/debug/report paths to read task activity rows
   Validation:
   - no active code reads or writes `comments.yaml`
   - recent verdict/note context still appears in prompts and CLI views

4. Align `StageReport` with the target activity/report model.
   Scope:
   - change `StageReport` from `stage` to canonical `pipeline_state`
   - narrow verdict usage so comments are not encoded as stage verdicts
   - remove `files_changed` from the canonical report shape
   Validation:
   - stage routing still works
   - report rendering still shows verdict summaries and warnings

5. Move recovery/stage report persistence off YAML.
   Scope:
   - store recovery reports and stage reports in SQLite-backed tables
   - remove `reports/*.yaml` and `recovery-*.yaml` as structured storage
   - keep plain-text logs only if they remain unstructured
   Validation:
   - recovery evidence and debug commands still surface the latest report data
   - no active code writes structured YAML reports

### Track B. Task storage and task status alignment

6. Stop using `task.yaml` as active task storage.
   Scope:
   - move incomplete-task durable state fully into SQLite
   - keep filesystem task directories only for artifacts/logs if still needed
   - remove filesystem reads from active task-loading code paths
   Validation:
   - queue/bootstrap/load paths work without reading `task.yaml`
   - `.litehive/config.yaml` is the only remaining Litehive-owned YAML file

7. Collapse terminal task statuses to the canonical domain model.
   Scope:
   - keep terminal task statuses as `done`, `flagged`, and `closed`
   - add `close_reason`
   - use `flag_reason` for merge/conflict/operator-attention cases
   - remove ad hoc terminal statuses such as `cancelled`, `wont_do`,
     `deferred`, `duplicate`, and `merge_failed`
   Validation:
   - status transitions, CLI output, and reporting use `close_reason` /
     `flag_reason`
   - merge failures no longer persist as `merge_failed`

### Track C. Pipeline and runtime model alignment

8. Define one canonical `PipelineState` and remove aliasing drift.
   Scope:
   - introduce the real internal machine-state enum in the domain layer
   - stop aliasing `PipelineState` to the coarse business-stage enum
   - map current lifecycle state holders onto the canonical pipeline-state type
   Validation:
   - prompts, transition rules, and persisted state all agree on one
     `PipelineState`

9. Split `TaskRuntime` into pipeline and execution slices.
   Scope:
   - introduce `PipelineRuntime` and `ExecutionRuntime`
   - move current flat runtime fields into the owning slice
   - keep `TaskRuntime` only as the container
   Validation:
   - subagent execution, retries, interruption, and outcome tracking still work
   - no flat runtime bucket remains for mixed concerns

10. Reconcile recovery naming with the chosen domain model.
    Scope:
    - decide and implement the final relationship between:
      - `FailureDiagnostics` and `FailureFingerprint`
      - `RecoveryRecord` / `RecoveryContext` and the current trigger/history
        structures
    - make the runtime/recovery surfaces use the chosen names consistently
    Validation:
    - recovery prompts, persistence, and routing all use one vocabulary

### Track D. Workspace-level YAML cleanup

11. Remove workspace-level monitoring YAML.
    Scope:
    - move `engine-monitoring.yaml` and pool summary YAML to SQLite or remove
      them if redundant
    - update status/diagnostic commands to read the new persistence path
    Validation:
    - no active code writes `engine-monitoring.yaml`
    - pool/status commands still render the same information

### Track E. Final terminology cleanup

12. Rename remaining artifact vocabulary to the selected target names.
    Scope:
    - `transcript` -> `execution trace` where the data is structured
    - `timeline` -> `event stream`
    - `journal` remains distinct from task activity
    Note:
    - plain-text artifact filenames can be handled last because they are lower
      risk than domain/storage changes
    Validation:
    - code, prompts, and docs use the same artifact terms

### Recommended execution order

- Start with Track A tasks 1-5.
- Then do Track B task 6 before broadening any more domain renames.
- After storage is stable, do Track B task 7 and Track C tasks 8-10.
- Finish with Track D task 11 and Track E task 12.

### Good Litehive task granularity

Each Litehive task should:

- change one conceptual boundary
- touch one main persistence surface at a time
- include a focused test slice in the task body
- avoid mixing naming cleanup with unrelated behavior changes

## Domain.md drift to reconcile

These are confirmed mismatches between `docs/domain.md` and the current code,
and they should be treated as real refactor work rather than documentation
nits.

### 1. Task status semantics still diverge from the domain model

Document target:

- `docs/domain.md` defines one terminal `closed` task status plus a distinct
  `close_reason`
- merge problems should route through `flagged` plus `flag_reason`, not become
  ad hoc task statuses

Current code:

- `litehive/domain/common.py` still includes task-status values like:
  - `merge_failed`
  - `cancelled`
  - `wont_do`
  - `deferred`
  - `duplicate`
- `litehive/domain/task.py` persists those values directly on `TaskRecord`
- `litehive/tasks/status.py` writes close outcomes into `task.status`
- `litehive/lifecycle/orchestration.py` still maps commit failure to
  `merge_failed`

Concrete actions:

- collapse terminal non-success task states into:
  - `closed`
  - `flagged`
- add canonical `close_reason` to the task model and storage
- stop encoding close outcomes as task statuses
- route merge failure through `flagged` with an explicit flag reason unless the
  domain model is revised
- update CLI/status/report output to read `close_reason` / `flag_reason`
  instead of inferring meaning from terminal task statuses

### 2. Pipeline vocabulary is still split across overlapping models

Document target:

- `docs/domain.md` defines one canonical `PipelineState` for the full internal
  machine state

Current code:

- `litehive/domain/common.py` uses `PipelineState` as an alias for the coarse
  business-stage `PipelineStatus`
- actual machine state lives elsewhere:
  - `LifecyclePhase`
  - `TaskState.stage`
  - runtime strings in `litehive/domain/runtime.py`
- event vocabulary in `litehive/lifecycle/events.py` still uses the current
  implementation names (`Pass`, `HookOk`, `CleanState`, etc.) rather than the
  document’s target surface

Concrete actions:

- define one canonical domain `PipelineState` that matches the real machine
  states
- separate it clearly from the coarse task-facing stage/status projection
- replace aliasing in `litehive/domain/common.py` with the real pipeline-state
  type
- reduce free-form runtime strings where they actually carry pipeline-state
  semantics
- either align the document’s event names with the implementation hierarchy or
  rename the implementation hierarchy to the canonical document vocabulary

### 3. Activity/report persistence still diverges from the document

Document target:

- `docs/domain.md` centers history on `ActivityEntry` / `TaskActivity` plus
  `StageReport` keyed by pipeline state

Current code:

- task discussion is still `TaskThreadComment`
- verdict history still lives in `comments.yaml`
- there is a separate Markdown `journal.md`
- `StageReport` is keyed by `TaskStage`, not by pipeline state
- `Verdict` still includes extra values beyond the tighter activity model

Concrete actions:

- move discussion/verdict persistence to an activity store
- delete `comments.yaml`
- decide whether `journal.md` survives as plain text or becomes activity/journal
  rows only
- align `StageReport` with the canonical activity model:
  - keyed by pipeline state if the document remains authoritative
  - or update `docs/domain.md` if task stage is the intended key
- narrow or split `Verdict` if the current enum is overloaded across activity,
  recovery, and lifecycle control signals

### 4. Recovery/runtime structure still diverges from the document

Document target:

- `docs/domain.md` expects:
  - `FailureDiagnostics`
  - `RecoveryRecord`
  - `RecoveryContext`
  - `TaskRuntime.pipeline`
  - `TaskRuntime.execution`

Current code:

- `litehive/domain/recovery.py` uses:
  - `FailureFingerprint`
  - `RecoveryTrigger`
  - `RecoveryOutcome`
  - `RecoveryDisposition`
- `litehive/domain/runtime.py` still uses one flat `TaskRuntime` carrying:
  - pipeline state
  - execution state
  - git state
  - hook-reject state
  - task outcome state

Concrete actions:

- decide whether `docs/domain.md` should adopt the implemented recovery model
  or whether code should be renamed/split to match the document
- split `TaskRuntime` into explicit pipeline and execution slices if
  `docs/domain.md` remains the target architecture
- move git/hook-reject/outcome concerns to explicit owned submodels rather than
  one flat runtime bucket
- reconcile recovery naming:
  - `FailureDiagnostics` vs `FailureFingerprint`
  - `RecoveryRecord` / `RecoveryContext` vs `RecoveryTrigger` /
    `RecoveryOutcome`

## Safe implementation order

### Phase 1. Prepare boundaries without renaming packages yet

- create repository/service facades for:
  - task
  - pipeline
  - activity
  - workspace
- move callers to those facades while keeping current modules in place
- stop adding new call sites to low-level helper modules

### Phase 2. Eliminate filesystem-era activity and direct CLI SQL

- add activity store abstraction
- migrate CLI/report/prompt/recovery paths off YAML artifacts
- migrate task/monitoring/session/report storage to SQLite
- migrate incomplete tasks and queue-backed task selection fully into SQLite
- unify pipeline storage access so CLI stops using raw SQL

### Phase 3. Simplify execution/runtime internals

- refactor `SubagentManager`
- split result classification and session writing
- move Heru-owned code out of Litehive
- move sandbox infra to the sandbox package

### Phase 4. Rename packages to match the domain model

- `lifecycle` -> `pipeline`
- `agents` -> `execution`
- `tasks` split into `task` / `activity` / `workspace`
- `state` split into `storage` / `workspace` / repository code
- remove `observability` umbrella
- rename CLI modules to plain names

### Phase 5. Delete superseded modules and compatibility code

- delete wrappers, dead prompt paths, hidden commands, dry-run/daemon
  duplicates, archive index support, and remaining compatibility helpers

## Open design choices to settle before implementation

- whether `task/` should stay plural (`tasks/`) for package-style consistency
  or go singular to match the domain naming
- whether backups belong in `storage/` or `workspace/`
- whether daemon code belongs under `workspace/` or `execution/`
- whether any structured artifact should remain on disk at all beyond plain
  text logs/transcripts
- whether debug/log helpers are supported operator tools or just developer
  maintenance tools that should be removed
- whether `StageReport` survives as a distinct persisted object or is reduced
  to a projection over activity + pipeline runtime data
