# Litehive test suite refactor

This document covers cleanup of the test tree after the package-structure refactor.

Current working-tree state:

- `tests/` has 38 active test files in a flat root
- `tests_integration/` has 12 active test files in a flat root
- `tests/workspace_helpers.py` is a 948-line catch-all support module
- `tests_integration/helpers.py` is a 433-line catch-all support module
- `pipeline_v2` still appears in 13 active test filenames even though the code now lives under `litehive.lifecycle`
- some names describe historical implementation details instead of current behavior
- the biggest files are too broad to move as-is:
  - `tests/test_tasks_and_subagents.py` at 3299 lines
  - `tests/test_workspace_bootstrap.py` at 798 lines
  - `tests/test_config.py` at 631 lines

## Goals

- align test locations with the current package layout
- rename files around behavior or current package ownership, not old architecture names
- split mixed files before moving them
- keep support modules as real helper code, not wrapper or re-export layers
- keep the suite behavioral; do not add repo-hygiene or forbidden-import tests

## Main problems

### 1. The suite is flatter than the production code

The application is now package-shaped:

- `agents/`
- `cli/`
- `config/`
- `domain/`
- `lifecycle/`
- `observability/`
- `recovery/`
- `state/`
- `tasks/`

The test tree is still mostly a historical flat pile. That makes it harder to find the test for a given production module, and it hides where overlap or duplication exists.

### 2. Several filenames still reflect deleted architecture

The main offenders are the `pipeline_v2` names:

- `tests/test_pipeline_v2_agent_retries.py`
- `tests/test_pipeline_v2_heru_factory.py`
- `tests/test_pipeline_v2_hook_config_wiring.py`
- `tests/test_pipeline_v2_hooks_and_commit.py`
- `tests/test_pipeline_v2_journal_cli.py`
- `tests/test_pipeline_v2_last_report.py`
- `tests/test_pipeline_v2_pre_exec_probe.py`
- `tests/test_pipeline_v2_prompt_serializer.py`
- `tests/test_pipeline_v2_sqlite_adapters.py`
- `tests/test_pipeline_v2_transitions.py`
- `tests/test_pipeline_v2_worktree_sync.py`
- `tests_integration/test_pipeline_v2_bootstrap.py`
- `tests_integration/test_pipeline_v2_end_to_end.py`

There are also names that are technically defensible but ambiguous:

- `tests/test_cli_root_fallback.py`
- `tests/test_status_broken_states.py`
- `tests/test_repair_performance.py`

### 3. Some files cover multiple domains

The clearest split candidates are:

- `tests/test_tasks_and_subagents.py`
  - task creation and dependency validation
  - state write / rollback behavior
  - subagent manager behavior
- `tests/test_workspace_bootstrap.py`
  - workspace bootstrap and registry
  - task runtime storage contract
  - engine monitoring observations
  - process profile scaffolding
- `tests/test_config.py`
  - workspace resolution
  - config loading and overlay behavior
  - engine/model selection
  - runner hook validation
- `tests/test_engine_variants_and_timeline.py`
  - Claude config defaults
  - task update CLI behavior
  - subagent timeline persistence
- `tests/test_rmtree_cleanup_logging.py`
  - task directory cleanup
  - daemon log pruning
- `tests/test_task_engine_cleanup.py`
  - task record validation
  - CLI help surface checks

### 4. Helper modules are too broad

`tests/workspace_helpers.py` currently acts as:

- fixture layer
- CLI wrapper layer
- task factory layer
- engine/subagent helper layer
- import re-export bag

That makes every test import large bundles of unrelated helpers and domain types. The same problem exists in smaller form in `tests_integration/helpers.py`.

## Target layout

Mirror the major production areas, not every leaf module:

```text
tests/
  agents/
  cli/
  config/
  domain/
  lifecycle/
  observability/
  recovery/
  state/
  tasks/
  support/

tests_integration/
  engines/
  lifecycle/
  sandbox/
  tasks/
  support/
```

Keep `tests/conftest.py` and `tests_integration/conftest.py` at the suite root.

Do not keep generic files like `test_misc.py` or broad names like `test_pipeline_v2_*`.

## Direct rename and move plan

These files can move with little or no test-body change.

### `tests/cli/`

- `tests/test_cli_root_fallback.py` -> `tests/cli/test_entrypoint.py`
- `tests/test_debug_command.py` -> `tests/cli/test_task_debug.py`
- `tests/test_list_and_show.py` -> `tests/cli/test_task_list_and_show.py`
- `tests/test_logs_command.py` -> `tests/cli/test_logs.py`
- `tests/test_main.py` -> `tests/cli/test_main_entrypoint.py`
- `tests/test_root_queue_recovery_help.py` -> removed as low-value help-copy coverage

### `tests/config/`

- `tests/test_engine_freeze.py` -> `tests/config/test_engine_freeze.py`

### `tests/domain/`

- `tests/test_feedback_cap.py` -> `tests/domain/test_feedback_cap.py`

### `tests/lifecycle/`

- `tests/test_pipeline_hook_reject_circuit_breaker.py` -> `tests/lifecycle/test_hook_reject_circuit_breaker.py`
- `tests/test_pipeline_v2_agent_retries.py` -> `tests/lifecycle/test_agent_retries.py`
- `tests/test_pipeline_v2_heru_factory.py` -> `tests/lifecycle/test_engine_adapter.py`
- `tests/test_pipeline_v2_hook_config_wiring.py` -> `tests/lifecycle/test_hook_config.py`
- `tests/test_pipeline_v2_hooks_and_commit.py` -> `tests/lifecycle/test_hooks_and_commit.py`
- `tests/test_pipeline_v2_journal_cli.py` -> `tests/lifecycle/test_journal_cli.py`
- `tests/test_pipeline_v2_last_report.py` -> `tests/lifecycle/test_last_report.py`
- `tests/test_pipeline_v2_pre_exec_probe.py` -> `tests/lifecycle/test_pre_exec_probe.py`
- `tests/test_pipeline_v2_prompt_serializer.py` -> `tests/lifecycle/test_prompt_serializer.py`
- `tests/test_pipeline_v2_sqlite_adapters.py` -> `tests/lifecycle/test_sqlite_adapters.py`
- `tests/test_pipeline_v2_transitions.py` -> `tests/lifecycle/test_transitions.py`
- `tests/test_pipeline_v2_worktree_sync.py` -> `tests/lifecycle/test_worktree_sync.py`

### `tests/observability/`

- `tests/test_attention_queue.py` -> `tests/observability/test_attention_queue.py`
- `tests/test_status_broken_states.py` -> `tests/observability/test_status_diagnostics.py`

### `tests/recovery/`

- `tests/test_recovery_runtime.py` -> `tests/recovery/test_runner_recovery.py`
- `tests/test_repair_performance.py` -> `tests/recovery/test_repair.py`

### `tests/state/`

- `tests/test_backup.py` -> `tests/state/test_backups.py`
- `tests/test_db_migrations.py` -> `tests/state/test_db_migrations.py`

### `tests/tasks/`

- `tests/test_archive.py` -> `tests/tasks/test_archive.py`
- `tests/test_flag_count_auto_defer.py` -> `tests/tasks/test_flag_auto_defer.py`
- `tests/test_slugify.py` -> `tests/tasks/test_slugify.py`
- `tests/test_task_close_active.py` -> `tests/tasks/test_close_active.py`
- `tests/test_task_comments.py` -> `tests/tasks/test_comments.py`

## Split before move

These files should not be moved as single large files. Split first, then relocate the smaller pieces.

### `tests/test_tasks_and_subagents.py`

Split into:

- `tests/tasks/test_create_task.py`
  - queue insertion
  - next-task numbering
  - dependency persistence
  - dependency validation
- `tests/state/test_task_persistence.py`
  - runtime save rollback
  - `workspace_transition_writes()` merge behavior
- `tests/agents/test_subagent_manager.py`
  - workspace root propagation
  - stage lookup
  - stdout/report parsing
  - run override precedence

### `tests/test_workspace_bootstrap.py`

Split into:

- `tests/config/test_workspace_bootstrap.py`
  - ensure layout
  - registry behavior
  - `LITEHIVE_HOME` handling
  - process profile scaffolding
- `tests/state/test_task_runtime_storage.py`
  - `task.yaml` intent-only contract
  - SQLite runtime loading
  - rejection of legacy runtime files
- `tests/observability/test_engine_monitoring.py`
  - `record_engine_execution()` observations and provider usage tracking

### `tests/test_config.py`

Split into:

- `tests/config/test_workspace_resolution.py`
- `tests/config/test_loading.py`
- `tests/config/test_engine_models.py`
- `tests/config/test_runner_hooks.py`

The current file mixes too many unrelated concerns for one location or one name.

### `tests/test_engine_variants_and_timeline.py`

Split into:

- `tests/agents/test_subagent_timeline.py`
- `tests/config/test_claude_settings.py`
- `tests/cli/test_task_update_engine_flags.py`

The current file mixes engine defaults, task update command behavior, and subagent timeline/report persistence.

### `tests/test_rmtree_cleanup_logging.py`

Split into:

- `tests/state/test_task_cleanup.py`
- `tests/observability/test_log_pruning.py`

### `tests/test_task_engine_cleanup.py`

Split into:

- `tests/cli/test_task_engine_help.py`

Then remove both resulting tests as low-value cleanup:

- `tests/cli/test_task_engine_help.py` only froze help output
- `tests/state/test_task_record_validation.py` only guarded a removed legacy shape

## Integration suite plan

### `tests_integration/engines/`

- `tests_integration/test_claude.py` -> `tests_integration/engines/test_claude.py`
- `tests_integration/test_codex.py` -> `tests_integration/engines/test_codex.py`
- `tests_integration/test_copilot.py` -> `tests_integration/engines/test_copilot.py`
- `tests_integration/test_gemini.py` -> `tests_integration/engines/test_gemini.py`
- `tests_integration/test_goz.py` -> `tests_integration/engines/test_goz.py`
- `tests_integration/test_opencode.py` -> `tests_integration/engines/test_opencode.py`

### `tests_integration/lifecycle/`

- `tests_integration/test_pipeline_v2_bootstrap.py` -> `tests_integration/lifecycle/test_bootstrap.py`
- `tests_integration/test_pipeline_v2_end_to_end.py` -> `tests_integration/lifecycle/test_end_to_end.py`

### `tests_integration/sandbox/`

- `tests_integration/test_sandbox_git_profiles.py` -> `tests_integration/sandbox/test_git_profiles.py`
- `tests_integration/test_sandbox_mock_engine.py` -> `tests_integration/sandbox/test_mock_engine.py`
- `tests_integration/test_sandboxed_engines.py` -> `tests_integration/sandbox/test_engine_invocations.py`

### `tests_integration/tasks/`

- `tests_integration/test_switching.py` -> `tests_integration/tasks/test_engine_switching.py`

## Helper cleanup

Keep helper cleanup pragmatic:

- move shared helper code under `tests/support/helpers.py`
- move integration helper code under `tests_integration/support/helpers.py`
- do not add wrapper modules that only re-export imports from those helper files
- keep reducing broad helper imports over time by importing production symbols directly where practical

Guideline: stop importing giant re-export bundles like `from tests.workspace_helpers import (...)` with dozens of unrelated names. Import production symbols directly unless the test is using real shared helper behavior.

## Low-value test smells

The smell to remove is not “CLI help tests” specifically. The real problem is tests that only freeze output text when no logic, state, branching, or data transformation is being exercised.

### Remove

- `tests/cli/test_task_engine_help.py`
  - only checks that Typer help text for `task add` and `task update` does not mention `--engine`
  - this does not exercise Litehive logic; it freezes framework-rendered help output
- `tests/tasks/test_archive.py::test_cmd_archive_no_args_prints_help`
  - only checks usage text and option names for a missing-argument invocation
  - this is presentation coverage, not archive behavior
- `tests/config/test_workspace_bootstrap.py::test_ensure_workspace_scaffolds_workspace_gitignore`
  - asserts exact scaffold lines in a generated `.gitignore`
  - this is static template coverage and fails on harmless wording or pattern edits
- `tests/config/test_workspace_bootstrap.py::test_ensure_workspace_scaffolds_profile_specific_context`
  - asserts prose and headings in generated `context.md`
  - this freezes editorial copy instead of behavior
- `tests/config/test_workspace_bootstrap.py::test_available_process_profiles_include_generic_and_project_templates`
  - asserts the full static list of shipped profile names
  - low signal as a behavioral test; profile resolution already covers the real config contract

### Keep

- `tests/cli/test_entrypoint.py`
  - exercises actual fallback behavior: run-next-task vs status
  - command-removal assertions cover command-surface behavior, not just wording
- `tests/lifecycle/test_journal_cli.py`
  - output depends on persisted state and journal transitions
  - this is a rendering contract for real lifecycle data, not static help copy
- `tests/observability/test_task_summary.py`
  - summary output depends on task reports and estimate calculations
  - the assertions verify behavior produced from state

## Rollout order

1. Create `tests/support/` and `tests_integration/support/` and move helper code first.
2. Move and rename the straightforward one-to-one files.
3. Split the mixed files and move the resulting files into their final directories.
4. Update `tests_integration/README.md` so examples use the new paths.
5. Run targeted suites for each moved area, then the full unit suite, then selected integration coverage.

## Verification

After the refactor, use this order:

1. `uv run pytest tests/cli tests/config tests/state -q`
2. `uv run pytest tests/lifecycle tests/tasks tests/recovery -q`
3. `uv run pytest tests -q`
4. `uv run pytest tests_integration/lifecycle tests_integration/sandbox tests_integration/tasks -q`
5. Engine smoke tests only when explicitly needed:
   - `uv run pytest tests_integration/engines/test_codex.py -q`
   - `uv run pytest tests_integration/engines/test_claude.py -q`

## Success criteria

This refactor is done when:

1. no active test filename contains `pipeline_v2`
2. test names describe current behavior instead of old architecture or ambiguous fallback wording
3. the large mixed files have been split along clear package boundaries
4. shared helpers live under `tests/support/` and `tests_integration/support/`
5. `tests/` and `tests_integration/` are organized by major production area
6. the regular unit suite stays green after the move
