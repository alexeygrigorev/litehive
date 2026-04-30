# Refactoring Audit

Date: 2026-04-30

This audit compares the current refactoring documents, especially
`docs/domain.md`, against the current codebase. It also records DRY, SOLID,
object-oriented design, and code smell findings as a task checklist.

Scope reviewed:

- `docs/domain.md`
- `docs/domain-spec.md`
- `docs/state-machine.md`
- `docs/refactoring-tasks.md`
- previous `docs/plans/` refactoring notes, which were removed because they
  described old or completed work
- current code under `litehive/domain`, `litehive/lifecycle`, `litehive/tasks`,
  `litehive/state`, `litehive/agents`, `litehive/observability`, `litehive/cli`,
  `litehive/daemon`, and `litehive/recovery`

## Compatibility Shim Audit

Current compatibility or previous-layout support still present in code:

- global-state migration from old config/data roots:
  `litehive/config/global_state.py`, `litehive/config/paths.py`
- legacy workspace-registry YAML migration:
  `litehive/config/registry.py`
- daemon registry YAML:
  `litehive/daemon/registry.py`,
  `litehive/observability/status_diagnostics.py`
- task activity YAML migration and fallback:
  `litehive/tasks/activity.py`
- legacy `task.yaml` import/migration and rebuild safety:
  `litehive/db/schema.py`, `litehive/state/rebuild_safety.py`
- legacy runtime YAML archive cleanup:
  `litehive/tasks/archive.py`
- hidden/deprecated CLI compatibility surfaces:
  `litehive/cli/app.py`, `litehive/cli/agent_cli.py`
- domain compatibility normalization:
  `litehive/domain/task.py`, `litehive/domain/runtime.py`
- prompt/debug compatibility references to materialized `session.yaml` and
  `report.yaml` labels:
  `litehive/lifecycle/prompt_serializer.py`,
  `litehive/cli/task_debug_support.py`
- tests that intentionally exercise old layouts or old YAML:
  `tests/config`, `tests/state`, `tests/tasks`, `tests/lifecycle`,
  `tests/observability`, and `tests/daemon`

Project rule: LiteHive does not maintain backwards compatibility for removed
file layouts, config keys, command aliases, or domain shapes. Compatibility
code should be deleted unless it is explicitly part of the current product
contract. The only LiteHive-owned workspace YAML file should be
`.litehive/config.yaml`.

## Priority 0 - Documentation Drift Blocking Good Refactor Tasks

- [x] Rewrite `docs/refactoring-tasks.md` so it only tracks active work. It
  currently lists several completed items as active, including active
  `task.yaml` storage, `PipelineState` aliasing, flat `TaskRuntime`, recovery
  naming, structured stage/recovery YAML, and `engine-monitoring.yaml`.
- [x] Update `docs/state-machine.md` to match current terminal status semantics.
  Removed terminal names are now described as `close_reason` values, and
  `merge_failed` is described as a `flag_reason`.
- [x] Update `docs/state-machine.md` flagged-task eligibility rules. The doc now
  points to current queue/recovery policy instead of claiming a simple status
  allowlist.
- [x] Fix the broken link in `docs/domain.md`: it references
  `domain.spec.md`, but the file is `docs/domain-spec.md`.
- [x] Replace illustrative model names in `docs/domain.md` that do not map to
  code. Examples include `Task`, `SubagentRun`, `Session`, `TaskService`, and
  `PipelineRunner`; current code uses names such as `TaskRecord`,
  `StateMachineRunner`, and lifecycle session types.
- [ ] Remove or reword all user-facing `v2` language. `docs/domain.md` says not
  to expose `v2`, but CLI help still says "v2 pipeline state machine"
  (`litehive/cli/app.py`, `litehive/cli/pipeline_cli.py`).

## Priority 1 - Single Source Of Truth And Domain Ownership

- [ ] Collapse the two execution-state models or make one an explicit
  projection. Current execution state exists in both lifecycle `TaskState` and
  `TaskRecord.runtime`, with bridge and sync code in
  `litehive/lifecycle/orchestration.py`. This is the largest consistency risk.
- [ ] Remove `TaskRuntime` legacy flat-payload compatibility once live data is
  migrated. `TaskRuntime` still normalizes flat legacy fields and proxies
  attribute access through `__getattr__`/`__setattr__`
  (`litehive/domain/runtime.py`).
- [ ] Decide the authoritative owner for retry, recovery, and failure history.
  Recovery data appears in lifecycle state and compact runtime projections; the
  split is documented, but still easy to misuse from callers.
- [ ] Make task transition ownership explicit. `litehive/tasks/status.py`
  contains state transition commands, metadata updates, runner termination,
  audit construction, queue mutation, recovery reset, and git/activity cleanup
  in one module.
- [ ] Extract a task application service from `litehive/tasks/status.py` so CLI
  and report paths call the same transition methods instead of coordinating
  persistence, audit, and queue state directly.
- [ ] Keep CLI handlers presentation-only. `litehive/cli/task_cli.py`,
  `litehive/cli/runner.py`, and `litehive/cli/agent_cli.py` should delegate
  domain actions instead of carrying business rules or compatibility behavior.

## Priority 1 - DRY Violations

- [ ] Consolidate runner and daemon lock metadata handling. `litehive/state/locking.py`
  and `litehive/daemon/registry.py` both manage process lock files, PID
  liveness, heartbeat metadata, stale clearing, and process-state persistence.
- [ ] Unify the status path. There is a fast entrypoint in `litehive/main.py`,
  broader status rendering in `litehive/observability/status.py`, workspace CLI
  status handling, and diagnostic loading in
  `litehive/observability/status_diagnostics.py`.
- [ ] Collapse duplicate daemon command surfaces. `litehive/cli/runner.py`
  overlaps with `litehive/cli/daemon_cli.py`; pick one public command path and
  make any alias a thin redirect or remove it.
- [ ] Consolidate worktree recovery, rescue, cleanup, and inspection. Related
  behavior is spread across `litehive/worktree.py`,
  `litehive/recovery/workspace_repair.py`, `litehive/cli/worktree_cli.py`, and
  lifecycle worktree sync nodes.
- [ ] Replace generated sandbox git-wrapper scripts with checked-in behavior.
  `litehive/agents/sandbox.py` writes wrapper scripts while
  `litehive/sandbox/git_wrapper.py` contains the real policy.
- [ ] Enforce one workspace YAML file: `.litehive/config.yaml`. All other
  LiteHive-owned workspace state should move to SQLite, JSONL, or plain text
  logs/artifacts. Current historical workspace data still includes runtime,
  report, session, recovery, attention, daemon registry, and pool-run YAML.
- [ ] Remove `update_task_metadata = update_task` from
  `litehive/tasks/status.py` and update importers to use one public name.
- [ ] Remove duplicate close/outcome vocabulary across `OutcomeKind`,
  `OutcomeReasonCode`, CLI choice strings, role prompts, and task status
  helpers. One canonical source should define accepted close outcomes.

## Priority 1 - SOLID And Object-Oriented Design Issues

- [ ] Split `litehive/tasks/status.py` by responsibility. It currently violates
  single responsibility by owning transition validation, signal handling, queue
  edits, audit records, journal messages, activity retraction, and persistence.
- [ ] Split `litehive/lifecycle/orchestration.py` into composition, state
  projection, terminal sync, and result rendering collaborators. The module
  wires dependencies and also performs domain state translation.
- [ ] Reduce `litehive/lifecycle/nodes/system.py` responsibilities. It contains
  generic system node abstractions, ready probes, worktree sync, git operations,
  pre-exec recovery, commit behavior, and merge-conflict handling.
- [ ] Make `ReadyNode` and `PreExecRecoveryNode` concrete for the current
  product. They use list-based probe/repair extension points even though
  production uses a narrow worktree-recovery path.
- [ ] Introduce explicit interfaces for persistence boundaries. Current callers
  use a mix of `state.store`, `state.records`, lifecycle persistence, and direct
  SQLite access.
- [ ] Separate domain models from persistence compatibility. Pydantic validators
  in domain records perform legacy normalization, which keeps old storage
  concerns inside the domain layer.
- [ ] Make command authorization a policy object instead of hard-coded parsing
  in `litehive/main.py`.
- [ ] Replace broad "manager" objects with smaller collaborators where behavior
  is cohesive, especially around `SubagentManager`, sandbox wrapping, and
  execution-session persistence.

## Priority 2 - Domain Vocabulary And Model Shape

- [ ] Finish verdict vocabulary alignment. `StageReportVerdict` uses
  `pass/reject/blocked`, while planning docs selected `accept/reject/blocked`
  and `Verdict` still includes broader task-activity values.
- [ ] Decide whether `StageReport.pipeline_state` should be the full canonical
  `PipelineState` or the current coarse `ReportPipelineState` alias. The field
  name is aligned, but the type is not fully canonical.
- [ ] Rename or retire `PipelineStatus` where it is only a UI projection. Make
  call sites visibly distinguish internal `PipelineState` from operator-facing
  status.
- [ ] Remove stale `PipelineState` and `TaskStatus` references from role prompt
  copy that imply old terminal statuses such as `duplicate`, `wont_do`, and
  `deferred` are task statuses instead of close reasons.
- [ ] Rename `lifecycle/` or update docs to stop disagreeing. Some plans say
  pipeline should become lifecycle; later plans suggest lifecycle should become
  pipeline. Pick one ubiquitous language and make docs and package names match.
- [ ] Replace remaining "agent compatibility alias" vocabulary with the current
  public command model (`litehive/cli/agent_cli.py`, `litehive/cli/app.py`).

## Priority 2 - Persistence And Compatibility Cleanup

- [ ] Delete `comments.yaml` and `thread.yaml` readers after any required
  migration. Activity writes SQLite today, but `litehive/tasks/activity.py`
  still reads and migrates both files.
- [ ] Delete all remaining runtime/report/session/recovery/pool-run/attention
  YAML producers and cleanup historical workspace YAML artifacts.
- [ ] Remove legacy `task.yaml` migration and rebuild-safety code when the
  supported migration window is over (`litehive/db/schema.py`,
  `litehive/state/rebuild_safety.py`).
- [ ] Remove legacy global-state and workspace registry migration paths once
  current installations have moved (`litehive/config/global_state.py`,
  `litehive/config/registry.py`).
- [ ] Replace status diagnostics fallback-to-default config behavior with
  explicit invalid-config reporting. `_load_config_for_status` currently
  creates a default config after validation failure.
- [ ] Audit broad `except Exception` handlers and convert them to typed
  exceptions or explicit diagnostic results. Hotspots include status
  diagnostics, config registry, lifecycle orchestration, daemon execution,
  attention scanning, and scope analysis.
- [ ] Stop silently returning empty lists or dictionaries for broken current
  state. Use typed empty values only when empty is genuinely valid.
- [ ] Remove hidden CLI compatibility paths once public replacements are stable.
  The hidden `agent` app and deprecated agent report role option should have a
  removal decision.

## Priority 2 - Package And Module Boundaries

- [ ] Replace the `state/` umbrella with clearer storage/repository ownership or
  document it as the persistence layer. Current `state` contains locks, records,
  backup, process locks, store, and rebuild safety.
- [ ] Split `tasks/` into task application logic, activity, queueing, archive,
  duplicate detection, and task paths. The package currently mixes domain
  behavior, persistence helpers, archive support, and reporting.
- [ ] Move report storage out of `tasks/reports.py` or rename the module to
  reflect its real scope: recovery evidence, activity rendering, report
  storage, artifact lookup, and report normalization.
- [ ] Clarify the Heru boundary. `litehive/agents`, `litehive/roles`, and
  `litehive/lifecycle/heru_factory.py` mix role prompts, engine construction,
  event capture, sandbox policy, and session storage.
- [ ] Decide whether `observability/` is one bounded context or multiple
  features. It currently contains engine monitoring, status rendering, status
  diagnostics, events, venv health, and log pruning.
- [ ] Move worktree-specific git code out of lifecycle nodes into a worktree
  service that can be shared by CLI rescue, runtime recovery, and pre-exec sync.

## Priority 3 - Design Smells And Maintainability

- [ ] Add module size budgets or review gates for modules above roughly 600 LOC.
  Current hotspots include `tasks/status.py`, `lifecycle/nodes/system.py`,
  `worktree.py`, `lifecycle/orchestration.py`, `tasks/queue.py`,
  `recovery/execution_recovery.py`, `observability/status.py`, and
  `lifecycle/prompt_serializer.py`.
- [ ] Reduce local imports inside functions where they are only avoiding cycles.
  Use this as a signal to fix package boundaries instead of normalizing the
  cycle.
- [ ] Replace stringly typed statuses with enums at boundaries. Many call sites
  still compare against raw strings for task status, execution status, close
  reason, flag reason, stages, and verdicts.
- [ ] Consolidate normalization helpers for task text, close outcomes, files
  changed, and acceptance criteria into clear value objects or services.
- [ ] Stop mixing audit construction with mutation methods. Mutators should
  return structured domain events or deltas that audit persistence can consume.
- [ ] Prefer explicit result objects over overloaded booleans and optional
  strings in recovery, worktree, and status diagnostics.
- [ ] Add architectural tests for forbidden dependencies: CLI must not reach
  direct SQLite, role prompt code must not mutate tasks, status rendering must
  not repair state, and domain validators must not read the filesystem.
- [ ] Add a dependency graph check to prevent new cycles between `tasks`,
  `state`, `lifecycle`, `agents`, and `observability`.
- [ ] Introduce a current-contract migration policy. Each legacy reader should
  state its removal trigger, owner, and validation command.
- [ ] Update tests that assert stale wording or compatibility aliases so they
  protect behavior instead of freezing obsolete API surface.

## Completed Or Mostly Completed Items To Mark As Historical

- [ ] Mark activity service boundary as completed. Active code uses
  `litehive/tasks/activity.py`.
- [ ] Mark thread/comment domain type rename as mostly completed. No active
  `TaskThreadComment`, `load_task_thread`, `save_task_thread`, or
  `append_thread_comment` references were found.
- [ ] Mark structured stage/recovery report YAML migration as completed. Reports
  are SQLite-backed.
- [ ] Mark active `task.yaml` task storage removal as completed, leaving only
  migration and rebuild-safety cleanup tasks.
- [ ] Mark real `PipelineState` introduction as completed. The enum exists in
  `litehive/domain/common.py`.
- [ ] Mark `TaskRuntime` pipeline/execution split as structurally completed,
  with follow-up work limited to legacy compatibility/proxy removal.
- [ ] Mark recovery vocabulary alignment as mostly completed. Remaining work is
  consistency enforcement and removing stale docs.
- [ ] Mark `engine-monitoring.yaml` active storage removal as completed.

## Suggested Execution Order

- [ ] First, rewrite the stale docs and refactoring queue so new tasks are not
  created from outdated assumptions.
- [ ] Next, create a small "current contract cleanup" batch: remove `v2`
  user-facing text, fix the domain doc link, and mark completed tasks
  historical.
- [ ] Then remove activity YAML compatibility (`comments.yaml` and
  `thread.yaml`) if migration safety allows.
- [ ] Then consolidate transition ownership in `litehive/tasks/status.py`.
- [ ] Then unify runner/daemon lock handling and status snapshot construction.
- [ ] Then tackle the largest design issue: one authoritative execution state
  model or an explicit projection boundary between lifecycle `TaskState` and
  `TaskRecord.runtime`.
