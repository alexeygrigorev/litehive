# Refactoring Tasks

Date: 2026-04-30

This is the active refactoring queue for aligning the codebase with
`docs/domain.md`, removing old compatibility code, and reducing DRY/SOLID
violations. Older planning material has been removed from `docs/plans/` because
it described package moves and storage migrations that have already landed or
no longer match the code.

Use `docs/refactoring-audit.md` for the broader audit notes. This file is the
actionable queue that should be decomposed into LiteHive tasks.

## Current Inventory

Already implemented:

- task intent and task runtime state are SQLite-backed
- `PipelineState` is a real enum in `litehive/domain/common.py`
- `TaskRuntime` is split into `pipeline` and `execution` slices
- stage and recovery reports are SQLite-backed
- engine monitoring is SQLite-backed
- structured subagent `session.yaml` and `report.yaml` artifacts are no longer
  the active model
- recovery naming mostly follows `FailureFingerprint`, `RecoveryTrigger`,
  `RecoveryOutcome`, and `recovery_history`
- active code uses `TaskActivityEntry`, `load_task_activity`, and
  `append_task_activity`; old thread/comment domain type names are gone
- `docs/domain.md` and `docs/state-machine.md` have been refreshed to describe
  the implemented status and storage model

Still outstanding:

- user-facing `v2` wording remains in CLI help
- activity still reads and migrates `comments.yaml` and `thread.yaml`
- legacy global-state, workspace-registry, task-yaml, and runtime compatibility
  paths still exist
- many old YAML files remain under `.litehive/tasks`, `.litehive/logs`, and
  stale artifact directories from earlier implementations
- lifecycle `TaskState` and `TaskRecord.runtime` both represent execution state
  and are bridged in orchestration code
- task transitions, status handling, queue mutation, audit construction, and
  runner/subagent termination are too concentrated in `litehive/tasks/status.py`
- runner and daemon process-lock handling are similar but separate
- status rendering and diagnostics have multiple paths
- worktree recovery, inspection, cleanup, and rescue logic have multiple owners

## Active Queue

### 1. Remove user-facing compatibility and `v2` CLI wording

Goal:

- stop exposing historical implementation names to operators

Scope:

- update `litehive/cli/app.py` pipeline help text
- update `litehive/cli/pipeline_cli.py` command help and messages
- review hidden `agent` command help and deprecated report role option
- decide whether the hidden `agent` compatibility app can be removed now or
  should become an explicit current API

Acceptance criteria:

- no operator-facing help text says `v2`
- hidden compatibility command surfaces are either removed or documented as
  current supported API

Suggested validation:

- `uv run litehive --help`
- `uv run litehive pipeline --help`
- `uv run litehive pipeline rules --help`
- `rg -n "v2|compatibility|Deprecated|deprecated" litehive/cli litehive/main.py`

### 2. Delete filesystem-era task activity compatibility

Goal:

- make SQLite task activity the only supported activity store

Scope:

- remove `comments.yaml` and `thread.yaml` readers from
  `litehive/tasks/activity.py`
- remove `migrate_legacy_task_activity_files`
- remove store bootstrap calls that migrate activity files
- remove corrupt-YAML activity fallback behavior
- update tests that still create or assert activity YAML files

Acceptance criteria:

- active code never reads or writes `comments.yaml` or `thread.yaml`
- task activity in prompts, reports, logs, and debug views still comes from
  SQLite

Suggested validation:

- `rg -n "comments\\.yaml|thread\\.yaml|migrate_legacy_task_activity" litehive tests`
- `uv run pytest tests/tasks tests/lifecycle/test_prompt_serializer.py tests/cli/test_agent_report.py tests/cli/test_task_debug.py`

### 3. Enforce the workspace YAML policy

Goal:

- ensure the only LiteHive-owned YAML file in a workspace is
  `.litehive/config.yaml`

Scope:

- remove stale runtime YAML artifacts from active workspace paths:
  - `runtime.yaml`
  - `task.yaml`
  - `comments.yaml`
  - `thread.yaml`
  - `report.yaml`
  - `session.yaml`
  - stage/recovery report YAML
  - pool-run YAML
  - attention item YAML
  - daemon/workspace registry YAML
- migrate any remaining active data to SQLite or append-only JSONL/text logs
- update `.litehive/.gitignore`, bootstrap behavior, archive cleanup, and repair
  tooling so new YAML files are not created
- review remaining packaged YAML templates under `litehive/cli/templates` and
  either document them as bootstrap templates or replace them with Python-backed
  defaults

Acceptance criteria:

- `find .litehive -type f \( -name '*.yaml' -o -name '*.yml' \)` returns only
  `.litehive/config.yaml` for a clean current workspace
- active code does not create any workspace YAML file other than
  `.litehive/config.yaml`
- tests cover that pool runs, attention items, reports, runtime state, and
  daemon/workspace registries do not use YAML

Suggested validation:

- `find .litehive -type f \( -name '*.yaml' -o -name '*.yml' \)`
- `rg -n "yaml|YAML|\\.ya?ml|safe_load|safe_dump" litehive tests`

### 4. Delete legacy global/task storage migration shims

Goal:

- remove previous-layout compatibility because LiteHive does not maintain
  backwards compatibility for removed storage shapes

Scope:

- remove legacy global-state migration from `litehive/config/global_state.py`
  and its invocation from `litehive/config/paths.py`
- remove legacy workspace-registry YAML migration from
  `litehive/config/registry.py`
- remove legacy `task.yaml` import/migration paths from `litehive/db/schema.py`
  after confirming current tests no longer depend on them
- remove `legacy_task_yaml_ids` rebuild-safety support from
  `litehive/state/rebuild_safety.py`
- remove tests that only preserve old layout behavior

Acceptance criteria:

- no active code migrates from old global YAML, workspace YAML, or task YAML
- current-shape bootstrap, registry, DB migration, and rebuild-safety tests pass

Suggested validation:

- `rg -n "legacy|deprecated|backward|compat|task\\.yaml|state\\.yaml|runtime\\.yaml|workspaces\\.yaml" litehive tests`
- `uv run pytest tests/config tests/state tests/tasks/test_create_task.py`

### 5. Remove domain-layer legacy normalization and proxy behavior

Goal:

- keep domain models focused on current state instead of silently accepting old
  shapes

Scope:

- remove `TaskRuntime` flat-payload normalization and compatibility attribute
  proxying
- remove terminal-status canonicalization that accepts removed task statuses as
  current input
- replace call sites that rely on raw strings or legacy values with current
  enums and explicit conversions at input boundaries

Acceptance criteria:

- domain validators reject old shapes through normal validation
- no domain model silently rewrites removed storage values

Suggested validation:

- `rg -n "legacy|_normalize_legacy|canonicalized|__getattr__|__setattr__" litehive/domain tests`
- `uv run pytest tests/domain tests/state tests/tasks/test_status_updates.py`

### 6. Consolidate task transition ownership

Goal:

- make task state changes easy to reason about and hard to duplicate

Scope:

- extract a task application service or transition module from
  `litehive/tasks/status.py`
- keep one implementation for close, abandon, park, resume, requeue, switch
  engine, and metadata update
- emit structured transition results or domain events that audit/journal code
  can persist
- make CLI and report handlers thin wrappers around the same service

Acceptance criteria:

- task transition logic has one owner
- audit and journal generation no longer obscure state mutations
- `update_task_metadata = update_task` is removed

Suggested validation:

- `uv run pytest tests/tasks/test_status_updates.py tests/tasks/test_close_active.py tests/cli/test_task_list_and_show.py tests/cli/test_agent_report.py`

### 7. Collapse to one authoritative execution state or explicit projection

Goal:

- remove the largest state-consistency risk in the codebase

Scope:

- decide whether lifecycle `TaskState` or `TaskRecord.runtime` is authoritative
- if both remain, make one a named projection with one-way update semantics
- remove bridge code that keeps two mutable models synchronized implicitly
- update lifecycle orchestration, persistence, status, prompt serialization, and
  recovery to use the chosen boundary

Acceptance criteria:

- one object owns execution state
- synchronization code in `litehive/lifecycle/orchestration.py` is either gone
  or reduced to explicit projection code
- stale state cannot survive in a second model after a lifecycle transition

Suggested validation:

- `uv run pytest tests/lifecycle tests/state/test_task_runtime_storage.py tests/recovery/test_runner_recovery.py`

### 8. Consolidate process-lock and status snapshot paths

Goal:

- remove duplicated runner/daemon/status logic

Scope:

- introduce one process-lock metadata utility for runner and daemon locks
- share PID liveness, heartbeat, stale clearing, metadata read/write, and
  process-state persistence
- build one status snapshot path used by fast status, full status, diagnostics,
  and workspace CLI output
- remove fallback-to-default config behavior in status diagnostics

Acceptance criteria:

- runner and daemon lock code use the same helper
- `litehive status` and full diagnostics share the same snapshot builder
- invalid current config is reported clearly and not replaced with defaults

Suggested validation:

- `uv run pytest tests/daemon tests/observability tests/cli/test_workspace_health.py`

### 9. Consolidate worktree ownership

Goal:

- make worktree behavior a single service used by lifecycle, recovery, and CLI

Scope:

- move worktree sync/rescue/cleanup/inspection decisions into one owner
- make lifecycle nodes delegate to that owner instead of running git workflow
  details directly
- make CLI code presentation-only
- unify concepts such as missing worktree, patch already landed, merge conflict,
  deferred metadata clear, and manual rescue

Acceptance criteria:

- worktree status and recovery decisions live in one module/service
- CLI rescue and lifecycle pre-exec sync agree on the same outcomes

Suggested validation:

- `uv run pytest tests/lifecycle/test_worktree_sync.py tests/cli/test_worktree_rescue.py tests/tasks/test_worktrees.py`

### 10. Replace generated sandbox git-wrapper duplication

Goal:

- keep one implementation of sandbox git policy

Scope:

- stop generating a second policy script in `litehive/agents/sandbox.py`
- reuse `litehive/sandbox/git_wrapper.py` directly or via a checked-in script
  template
- remove duplicate policy strings and tests that only preserve generated script
  text

Acceptance criteria:

- destructive git policy is implemented once
- sandbox setup mounts or invokes the shared implementation

Suggested validation:

- `uv run pytest tests/agents/test_sandbox_integration.py`
