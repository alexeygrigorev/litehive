# Object Refactor Plan

This checklist sequences the refactor described in:

- `docs/function-method-analysis-2026-05-08.md`
- `docs/proposed-object-structure-2026-05-08.md`
- `docs/code-style.md`

The goal is not to convert every function into a method. The goal is to move
workspace-bound, persistence-bound, and policy-bound behavior onto cohesive
objects while keeping pure utility functions free.

## Rules For Every Slice

- Start from a clean understanding of the exact old function path.
- Add or verify focused characterization tests before moving behavior.
- Introduce the new object with constructor-injected dependencies.
- Route a small set of callers to the object.
- Keep old functions only as temporary wrappers while callers migrate.
- Delete wrappers only when `rg` confirms no production callers remain.
- Run focused tests for the touched package.
- Run `make typecheck`.
- Run `make test` before any commit.
- Do not modify lint, formatter, pyrefly, ruff, or CI config.

## Phase 0: Guardrails And Baseline

- [x] P0.1. Add a short doc note in `docs/code-style.md` that service
  extraction should happen in small tested slices with temporary wrappers only
  during caller migration.

  Verification:
  - [x] `rg -n "temporary wrapper|tested refactor slices|constructor" docs/code-style.md`
  - [x] `uv run pytest tests/test_architecture_guardrails.py -q`

  Completed 2026-05-08: added the Dependency Injection note in
  `docs/code-style.md` requiring service extraction in tested refactor
  slices, constructor-injected services, small caller routing, and temporary
  wrappers only while callers migrate. Verified with the exact `rg` command
  above and `uv run pytest tests/test_architecture_guardrails.py -q`.

- [x] P0.2. Add a lightweight architecture guardrail if one does not already
  exist: new internal functions below CLI/process boundary should not accept
  raw `root: Path` when `Workspace` or a service would do.

  Verification:
  - [x] `uv run pytest tests/test_architecture_guardrails.py -q`
  - [x] `make typecheck`

  Completed 2026-05-08: added
  `test_internal_functions_do_not_add_raw_workspace_root_parameters` in
  `tests/test_architecture_guardrails.py`. It excludes CLI/process,
  container, config path/workspace, db schema, git ops, and sandbox wrapper
  boundaries, then froze the then-current internal raw-root debt in
  `litehive/tasks/paths.py` and `litehive/lifecycle/nodes/system.py`. Later
  continuation slices RD2-RD9 cleared those allowances, so the guardrail now
  has an empty internal raw-root allowance list. Verified with the exact
  focused architecture test and `make typecheck`.

- [x] P0.3. Add a checklist item to this file whenever a new object is created
  and its old wrappers still exist.

  Verification:
  - [x] Every temporary wrapper has a matching unchecked cleanup item in this
    document.

  Completed 2026-05-08: added the `Temporary Wrapper Cleanup Ledger` section
  below, with a required unchecked cleanup template naming the old wrapper,
  new method, and `rg` deletion proof. Later slices populated the ledger and
  then completed every wrapper cleanup item, so it now records the full
  created-and-deleted wrapper history for this plan.


## Temporary Wrapper Cleanup Ledger

Add one unchecked item here whenever a refactor introduces or preserves an old
free function as a temporary wrapper around a new object method. The item must
name the wrapper function, the new method, and the `rg` command that will prove
it is safe to delete.

Wrappers created and cleaned up by this plan:

- [x] TW1. Wrapper `bootstrap_runtime_settings` delegates to
  `RuntimeSettingsRepository.bootstrap`.

  Delete when:
  - [x] `rg -n "bootstrap_runtime_settings\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `RuntimeSettingsRepository.bootstrap`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: wrapper deleted during P1.4; the `rg` command now has
  no matches.

- [x] TW2. Wrapper `load_runtime_settings` delegates to
  `RuntimeSettingsRepository.load`.

  Delete when:
  - [x] `rg -n "load_runtime_settings\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `RuntimeSettingsRepository.load`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: wrapper deleted during P1.4; the `rg` command now has
  no matches.

- [x] TW3. Wrapper `apply_runtime_settings_to_config_data` delegates to
  `RuntimeSettingsRepository.apply_to_config_data`.

  Delete when:
  - [x] `rg -n "apply_runtime_settings_to_config_data\\(" litehive tests`
    shows only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `RuntimeSettingsRepository.apply_to_config_data`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: wrapper deleted during P1.4; the `rg` command now has
  no matches.

- [x] TW4. Wrapper `set_runtime_setting` delegates to
  `RuntimeSettingsRepository.set`.

  Delete when:
  - [x] `rg -n "set_runtime_setting\\(" litehive tests` shows only the wrapper
    definition and intentional compatibility tests.
  - [x] Production callers use `RuntimeSettingsRepository.set`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: wrapper deleted during P1.4; the `rg` command now has
  no matches.

- [x] TW5. Wrapper `load_runtime_setting_audit_entries` delegates to
  `RuntimeSettingsRepository.audit_entries`.

  Delete when:
  - [x] `rg -n "load_runtime_setting_audit_entries\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `RuntimeSettingsRepository.audit_entries`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: wrapper deleted during P1.4; the `rg` command now has
  no matches.

- [x] TW6. Wrapper `ensure_runtime_ignored_for_workspace` delegates to
  `WorkspaceTasks.ensure_runtime_ignored`.

  Delete when:
  - [x] `rg -n "ensure_runtime_ignored_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `WorkspaceTasks.ensure_runtime_ignored`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Internal `records.py` write paths
  now call `_ensure_runtime_ignored_for_workspace_impl(...)` directly, while
  `state.persist` and `state.locking` call
  `workspace_tasks_for_workspace(workspace).ensure_runtime_ignored()`. Verified
  the exact `rg` command has no matches, then ran focused state tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW7. Wrapper `write_task_runtime_for_workspace` delegates to
  `WorkspaceTasks.write_runtime`.

  Delete when:
  - [x] `rg -n "write_task_runtime_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `WorkspaceTasks.write_runtime`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. The remaining internal guarded
  runtime-save path calls `_write_task_runtime_for_workspace_impl(...)`
  directly, and repository characterization tests call
  `workspace_tasks_for_workspace(workspace).write_runtime(task)`. Verified the
  exact `rg` command has no matches, then ran
  `uv run pytest tests/state/test_task_repository_characterization.py -q`,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW8. Wrapper `save_task_runtime_for_workspace` delegates to
  `WorkspaceTasks.save_runtime`.

  Delete when:
  - [x] `rg -n "save_task_runtime_for_workspace\\(" litehive tests` shows only
    the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `WorkspaceTasks.save_runtime`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Unit and integration callers now
  use `workspace_tasks_for_workspace(workspace).save_runtime(task)`. Verified
  no matches for `rg -n "save_task_runtime_for_workspace\\(" litehive tests
  tests_integration`, then ran focused runtime/log tests, `make typecheck`,
  and `make test` (`940 passed, 1 skipped`).

- [x] TW9. Wrapper `create_task_for_workspace` delegates to
  `WorkspaceTasks.create`.

  Delete when:
  - [x] `rg -n "create_task_for_workspace\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `WorkspaceTasks.create`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Production already had no callers;
  tests and integration helpers now create tasks through
  `workspace_tasks_for_workspace(workspace).create(...)`. Verified no matches
  for `rg -n "create_task_for_workspace\\(" litehive tests tests_integration`,
  then ran focused task/daemon tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW10. Wrapper `discard_created_task_for_workspace` delegates to
  `WorkspaceTasks.discard_created`.

  Delete when:
  - [x] `rg -n "discard_created_task_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `WorkspaceTasks.discard_created`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Remaining tests use
  `workspace_tasks_for_workspace(workspace).discard_created(task_id)`.
  Verified no matches for the exact `rg` command, then ran focused cleanup,
  event-log, and repository tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW11. Wrapper `list_tasks_for_workspace` delegates to
  `WorkspaceTasks.list`.

  Delete when:
  - [x] `rg -n "list_tasks_for_workspace\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `WorkspaceTasks.list`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Remaining callers use
  `workspace_tasks_for_workspace(workspace).list(...)`; tests that asserted
  clean repair does not scan tasks now monkeypatch `WorkspaceTasks.list`
  directly. Verified no matches for the exact `rg` command, then ran focused
  task/state/recovery tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW12. Wrapper `get_task_for_workspace` delegates to
  `WorkspaceTasks.get`.

  Delete when:
  - [x] `rg -n "get_task_for_workspace\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `WorkspaceTasks.get`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Internal `records.py` paths now
  call `_get_task_for_workspace_impl(...)`; tests and integration helpers use
  `workspace_tasks_for_workspace(workspace).get(task_id)`. Verified no matches
  for `rg -n "get_task_for_workspace\\(" litehive tests tests_integration`,
  then ran focused task/state/recovery tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW13. Wrapper `get_task_record_for_workspace` delegates to
  `WorkspaceTasks.get_record`.

  Delete when:
  - [x] `rg -n "get_task_record_for_workspace\\(" litehive tests` shows only
    the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `WorkspaceTasks.get_record`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Internal follow-up creation uses
  `_get_task_record_for_workspace_impl(...)`; tests use
  `workspace_tasks_for_workspace(workspace).get_record(task_id)`. Verified no
  matches for `rg -n "get_task_record_for_workspace\\(" litehive tests
  tests_integration`, then ran focused config/agent-report/launch-state tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW14. Wrapper `require_task_for_workspace` delegates to
  `WorkspaceTasks.require`.

  Delete when:
  - [x] `rg -n "require_task_for_workspace\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `WorkspaceTasks.require`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Tests and integration helpers use
  `workspace_tasks_for_workspace(workspace).require(task_id)`. Verified no
  matches for `rg -n "require_task_for_workspace\\(" litehive tests
  tests_integration`, then ran focused task/status/runtime tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW15. Wrapper `save_task_for_workspace` delegates to
  `WorkspaceTasks.save`.

  Delete when:
  - [x] `rg -n "save_task_for_workspace\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `WorkspaceTasks.save`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Tests and integration helpers use
  `workspace_tasks_for_workspace(workspace).save(task)`. Verified no matches
  for `rg -n "save_task_for_workspace\\(" litehive tests tests_integration`,
  then ran focused task/lifecycle/CLI tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW16. Wrapper `mark_task_run_started_for_workspace` delegates to
  `TaskRuntimeTransitions.start_run`.

  Delete when:
  - [x] `rg -n "mark_task_run_started_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.start_run`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Tests now use
  `task_runtime_transitions_for_workspace(workspace).start_run(task)`.
  Verified no matches for `rg -n "mark_task_run_started_for_workspace\\("
  litehive tests tests_integration`, then ran focused runtime/close tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW17. Wrapper `mark_task_run_finished_for_workspace` delegates to
  `TaskRuntimeTransitions.finish_run`.

  Delete when:
  - [x] `rg -n "mark_task_run_finished_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.finish_run`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. The runtime update
  characterization test now calls
  `task_runtime_transitions_for_workspace(workspace).finish_run(...)`
  directly. Verified no matches for `rg -n
  "mark_task_run_finished_for_workspace\\(" litehive tests tests_integration`,
  then ran focused runtime/close tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW18. Wrapper `finish_task_run_transition_for_workspace` delegates to
  `TaskRuntimeTransitions.finish_run_transition`.

  Delete when:
  - [x] `rg -n "finish_task_run_transition_for_workspace\\(" litehive tests`
    shows only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.finish_run_transition`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Runtime and flag auto-defer tests
  now call
  `task_runtime_transitions_for_workspace(workspace).finish_run_transition(...)`
  directly. Verified no matches for `rg -n
  "finish_task_run_transition_for_workspace\\(" litehive tests
  tests_integration`, then ran focused runtime/flag tests, `make typecheck`,
  and `make test` (`940 passed, 1 skipped`).

- [x] TW19. Wrapper `set_task_retry_state_for_workspace` delegates to
  `TaskRuntimeTransitions.set_retry_state`.

  Delete when:
  - [x] `rg -n "set_task_retry_state_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.set_retry_state`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. It had no remaining production
  or test callers; retry-state persistence is owned by
  `TaskRuntimeTransitions.set_retry_state`. Verified no matches for `rg -n
  "set_task_retry_state_for_workspace\\(" litehive tests tests_integration`,
  then ran focused runtime/flag tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW20. Wrapper `clear_task_outcome_for_workspace` delegates to
  `TaskRuntimeTransitions.clear_outcome`.

  Delete when:
  - [x] `rg -n "clear_task_outcome_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.clear_outcome`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. It had no remaining production
  or test callers; outcome clearing is owned by
  `TaskRuntimeTransitions.clear_outcome`. Verified no matches for `rg -n
  "clear_task_outcome_for_workspace\\(" litehive tests tests_integration`,
  then ran focused runtime/flag tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW21. Wrapper `mark_task_outcome_for_workspace` delegates to
  `TaskRuntimeTransitions.record_outcome`.

  Delete when:
  - [x] `rg -n "mark_task_outcome_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.record_outcome`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. The runtime update
  characterization test now calls
  `task_runtime_transitions_for_workspace(workspace).record_outcome(...)`
  directly. Verified no matches for `rg -n
  "mark_task_outcome_for_workspace\\(" litehive tests tests_integration`,
  then ran focused runtime/flag tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW22. Wrapper `mark_stage_started_for_workspace` delegates to
  `TaskRuntimeTransitions.start_stage`.

  Delete when:
  - [x] `rg -n "mark_stage_started_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.start_stage`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Runtime update tests now call
  `task_runtime_transitions_for_workspace(workspace).start_stage(...)`
  directly. Verified no matches for `rg -n
  "mark_stage_started_for_workspace\\(" litehive tests tests_integration`,
  then ran focused runtime tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW23. Wrapper `mark_stage_finished_for_workspace` delegates to
  `TaskRuntimeTransitions.finish_stage`.

  Delete when:
  - [x] `rg -n "mark_stage_finished_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.finish_stage`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Runtime update tests now call
  `task_runtime_transitions_for_workspace(workspace).finish_stage(...)`
  directly. Verified no matches for `rg -n
  "mark_stage_finished_for_workspace\\(" litehive tests tests_integration`,
  then ran focused runtime tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW24. Wrapper `mark_subagent_started_for_workspace` delegates to
  `TaskRuntimeTransitions.mark_subagent_started`.

  Delete when:
  - [x] `rg -n "mark_subagent_started_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.mark_subagent_started`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Production was already using
  `task_runtime_transitions_for_workspace(self.workspace).mark_subagent_started(...)`
  in `SubagentManager`; remaining tests now use
  `task_runtime_transitions_for_workspace(workspace).mark_subagent_started(...)`.
  Verified no matches for `rg -n "mark_subagent_started_for_workspace\\("
  litehive tests tests_integration`, then ran focused runtime/close/subagent
  event-stream tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW25. Wrapper `mark_subagent_pid_for_workspace` delegates to
  `TaskRuntimeTransitions.mark_subagent_pid`.

  Delete when:
  - [x] `rg -n "mark_subagent_pid_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.mark_subagent_pid`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Production was already using
  `task_runtime_transitions_for_workspace(self.workspace).mark_subagent_pid(...)`
  in the subagent session layer; the remaining close-active test now calls the
  service method directly. Verified no matches for `rg -n
  "mark_subagent_pid_for_workspace\\(" litehive tests tests_integration`, then
  ran focused close/subagent event-stream tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW26. Wrapper `mark_subagent_progress_for_workspace` delegates to
  `TaskRuntimeTransitions.mark_subagent_progress`.

  Delete when:
  - [x] `rg -n "mark_subagent_progress_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.mark_subagent_progress`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Production was already using
  `task_runtime_transitions_for_workspace(self.workspace).mark_subagent_progress(...)`
  in `SubagentManager`; the remaining runtime test now calls the service
  method directly. Verified no matches for `rg -n
  "mark_subagent_progress_for_workspace\\(" litehive tests tests_integration`,
  then ran focused runtime/subagent event-stream tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW27. Wrapper `mark_subagent_finished_for_workspace` delegates to
  `TaskRuntimeTransitions.mark_subagent_finished`.

  Delete when:
  - [x] `rg -n "mark_subagent_finished_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.mark_subagent_finished`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Production was already using
  `task_runtime_transitions_for_workspace(self.workspace).mark_subagent_finished(...)`
  in `SubagentManager`; the remaining runtime test now calls the service
  method directly. Verified no matches for `rg -n
  "mark_subagent_finished_for_workspace\\(" litehive tests tests_integration`,
  then ran focused runtime/subagent event-stream tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW28. Wrapper `mark_engine_switch_for_workspace` delegates to
  `TaskRuntimeTransitions.switch_engine`.

  Delete when:
  - [x] `rg -n "mark_engine_switch_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskRuntimeTransitions.switch_engine`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. Production engine switching was
  already using
  `task_runtime_transitions_for_workspace(workspace).switch_engine(...)`; the
  remaining runtime test now calls the service method directly. Verified no
  matches for `rg -n "mark_engine_switch_for_workspace\\(" litehive tests
  tests_integration`, then ran focused runtime/engine-freeze tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW29. Wrapper `enqueue_task_for_workspace` delegates to
  `TaskQueueService.enqueue`.

  Delete when:
  - [x] `rg -n "enqueue_task_for_workspace\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.enqueue`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. The queue
  mutation characterization test now calls
  `task_queue_service_for_workspace(workspace).enqueue(...)` directly. Verified
  no matches for `rg -n "enqueue_task_for_workspace\\("
  litehive/tasks/queue.py tests tests_integration`; the remaining matches in
  `litehive/tasks/queue_mutations.py` are the lower-level implementation that
  `TaskQueueService.enqueue` still owns. Ran focused queue mutation tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW30. Wrapper `enqueue_task_front_for_workspace` delegates to
  `TaskQueueService.enqueue`.

  Delete when:
  - [x] `rg -n "enqueue_task_front_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.enqueue`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. The queue
  mutation characterization test now calls
  `task_queue_service_for_workspace(workspace).enqueue(..., front=True)`
  directly. Verified no matches for `rg -n
  "enqueue_task_front_for_workspace\\(" litehive/tasks/queue.py tests
  tests_integration`; the remaining match in `litehive/tasks/queue_mutations.py`
  is the lower-level implementation that `TaskQueueService.enqueue` still
  owns. Ran focused queue mutation tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW31. Wrapper `move_queued_task_for_workspace` delegates to
  `TaskQueueService.move`.

  Delete when:
  - [x] `rg -n "move_queued_task_for_workspace\\(" litehive tests` shows only
    the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.move`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. The queue
  CLI, engine-switch flow, and queue mutation tests now call
  `task_queue_service_for_workspace(...).move(...)` directly. Verified no
  matches for `rg -n "move_queued_task_for_workspace\\("
  litehive/tasks/queue.py litehive/cli litehive/tasks/switch_engine.py tests
  tests_integration`; the remaining match in `litehive/tasks/queue_mutations.py`
  is the lower-level implementation that `TaskQueueService.move` still owns.
  Ran focused queue/CLI/engine tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW32. Wrapper `prioritize_queued_tasks_for_workspace` delegates to
  `TaskQueueService.prioritize`.

  Delete when:
  - [x] `rg -n "prioritize_queued_tasks_for_workspace\\(" litehive tests`
    shows only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.prioritize`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. The queue
  CLI and queue mutation tests now call
  `task_queue_service_for_workspace(...).prioritize(...)` directly. Verified
  no matches for `rg -n "prioritize_queued_tasks_for_workspace\\("
  litehive/tasks/queue.py litehive/cli tests tests_integration`; the remaining
  match in `litehive/tasks/queue_mutations.py` is the lower-level
  implementation that `TaskQueueService.prioritize` still owns. Ran focused
  queue/CLI tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW33. Wrapper `drop_task_from_workspace_state` delegates to
  `TaskQueueService.remove_from_state`.

  Delete when:
  - [x] `rg -n "drop_task_from_workspace_state\\(" litehive tests` shows only
    the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.remove_from_state`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. Status
  close, completed-task recovery, and launch-state recovery tests now call
  `TaskQueueService.remove_from_state(...)` directly. Verified no matches for
  `rg -n "drop_task_from_workspace_state\\(" litehive/tasks/queue.py
  litehive/tasks/status_close.py litehive/tasks/completed_task_recovery.py
  tests tests_integration`; the remaining match in
  `litehive/tasks/queue_mutations.py` is the lower-level implementation that
  `TaskQueueService.remove_from_state` still owns. Ran focused
  close/recovery/queue lifecycle tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW34. Wrapper `set_active_task` delegates to
  `TaskQueueService.mark_active`.

  Delete when:
  - [x] `rg -n "set_active_task\\(" litehive tests` shows only the wrapper
    definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.mark_active`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. Close-active
  tests, agent mutation tests, and integration helpers now call
  `task_queue_service_for_workspace(...).mark_active(...)` directly. Verified
  no matches for `rg -n "set_active_task\\(" litehive/tasks/queue.py tests
  tests_integration`; the remaining matches in `litehive/tasks/queue_selection.py`
  are the lower-level implementation that `TaskQueueService.mark_active` still
  owns. Ran focused close/agent mutation/queue invariant tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW35. Wrapper `clear_active_task` delegates to
  `TaskQueueService.clear_active`.

  Delete when:
  - [x] `rg -n "clear_active_task\\(" litehive tests` shows only the wrapper
    definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.clear_active`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. It had no
  public callers; active-task clearing is owned by
  `TaskQueueService.clear_active`. Verified no matches for `rg -n
  "clear_active_task\\(" litehive/tasks/queue.py tests tests_integration`; the
  remaining match in `litehive/tasks/queue_selection.py` is the lower-level
  implementation that `TaskQueueService.clear_active` still owns. Ran focused
  queue invariant/close-active tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW36. Wrapper `restore_untouched_active_task` delegates to
  `TaskQueueService.restore_untouched_active`.

  Delete when:
  - [x] `rg -n "restore_untouched_active_task\\(" litehive tests` shows only
    the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.restore_untouched_active`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. It had no
  public callers; untouched active-task restoration is owned by
  `TaskQueueService.restore_untouched_active`. Verified no matches for
  `rg -n "restore_untouched_active_task\\(" litehive/tasks/queue.py tests
  tests_integration`; the remaining match in `litehive/tasks/queue_selection.py`
  is the lower-level implementation that the service still owns. Ran focused
  queue invariant/regression tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW37. Wrapper `active_task_markers_for_workspace` delegates to
  `TaskQueueService.active_task_markers`.

  Delete when:
  - [x] `rg -n "active_task_markers_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.active_task_markers`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. Stop
  handling and runner-lock mutation guards now call
  `task_queue_service_for_workspace(...).active_task_markers(...)` directly.
  Verified no matches for `rg -n "active_task_markers_for_workspace\\("
  litehive/tasks/queue.py litehive/tasks/stop.py litehive/state/locking.py
  tests tests_integration`; the remaining matches in
  `litehive/tasks/queue_selection.py` are the lower-level implementation and
  its internal self-use. Ran focused stop/queue tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW38. Wrapper `validate_single_active_task_for_workspace` delegates to
  `TaskQueueService.validate_single_active_task`.

  Delete when:
  - [x] `rg -n "validate_single_active_task_for_workspace\\(" litehive tests`
    shows only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.validate_single_active_task`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. Stop
  handling now calls
  `task_queue_service_for_workspace(...).validate_single_active_task(...)`
  directly. Verified no matches for `rg -n
  "validate_single_active_task_for_workspace\\(" litehive/tasks/queue.py
  litehive/tasks/stop.py tests tests_integration`; the remaining matches in
  `litehive/tasks/queue_selection.py` are the lower-level implementation and
  internal queue-selection calls. Ran focused stop/queue tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW39. Wrapper `peek_next_task` delegates to
  `TaskQueueService.peek_next`.

  Delete when:
  - [x] `rg -n "peek_next_task\\(" litehive tests` shows only the wrapper
    definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.peek_next`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. The DB
  migration characterization test now calls
  `task_queue_service_for_workspace(workspace).peek_next()` directly. Verified
  no matches for `rg -n "peek_next_task\\(" litehive/tasks/queue.py tests
  tests_integration`; the remaining match in `litehive/tasks/queue_selection.py`
  is the lower-level implementation that `TaskQueueService.peek_next` still
  owns. Ran focused DB migration/queue tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW40. Wrapper `peek_next_task_selection` delegates to
  `TaskQueueService.peek_next_selection`.

  Delete when:
  - [x] `rg -n "peek_next_task_selection\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.peek_next_selection`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. The runner
  peek path and queue invariant/regression tests now call
  `task_queue_service_for_workspace(...).peek_next_selection()` directly.
  Verified no matches for `rg -n "peek_next_task_selection\\("
  litehive/tasks/queue.py litehive/cli/runner.py tests tests_integration`; the
  remaining matches in `litehive/tasks/queue_selection.py` are the lower-level
  implementation and its internal self-use. Ran focused queue/daemon CLI tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW41. Wrapper `dequeue_next_task` delegates to
  `TaskQueueService.dequeue_next`.

  Delete when:
  - [x] `rg -n "dequeue_next_task\\(" litehive tests` shows only the wrapper
    definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.dequeue_next`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. The root CLI
  callback, daemon task execution, runner drain seam, lifecycle tests, launch
  recovery tests, and parked lifecycle tests now call
  `task_queue_service_for_workspace(...).dequeue_next()` directly, or use the
  named `pick_next_task` seam that delegates to that service method. Verified
  no matches for `rg -n "dequeue_next_task\\(" litehive/tasks/queue.py
  litehive/cli litehive/daemon tests tests_integration`; the remaining match
  in `litehive/tasks/queue_selection.py` is the lower-level implementation
  that `TaskQueueService.dequeue_next` still owns. Ran focused
  CLI/lifecycle/parked-task tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW42. Wrapper `dequeue_next_task_selection` delegates to
  `TaskQueueService.select_next`.

  Delete when:
  - [x] `rg -n "dequeue_next_task_selection\\(" litehive tests` shows only
    the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.select_next`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py` and removed its stale `__all__` entry. Queue
  invariant/regression tests now call
  `task_queue_service_for_workspace(...).select_next()` directly. Verified no
  matches for `rg -n "dequeue_next_task_selection\\(" litehive/tasks/queue.py
  tests tests_integration`; the remaining matches in
  `litehive/tasks/queue_selection.py` are the lower-level implementation and
  its internal self-use. Ran focused queue tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW43. Wrapper `restore_missing_queued_tasks` delegates to
  `TaskQueueService.restore_missing_from_state`.

  Delete when:
  - [x] `rg -n "restore_missing_queued_tasks\\(" litehive tests` shows only
    the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskQueueService.restore_missing_from_state`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the public wrapper from
  `litehive/tasks/queue.py`, removed its stale `__all__` entry, and removed
  the unused `TaskQueueService.restore_missing_queued_tasks` alias. The parked
  lifecycle test now calls `TaskQueueService.restore_missing_from_state(...)`
  directly. Verified no matches for `rg -n "restore_missing_queued_tasks\\("
  litehive/tasks/queue.py tests tests_integration`; the remaining matches in
  `litehive/tasks/queue_selection.py` are the lower-level implementation and
  its internal self-use. Ran focused parked/queue tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW44. Wrapper `task_stage_outcomes_for_workspace` delegates to
  `PoolService.stage_outcomes`.

  Delete when:
  - [x] `rg -n "task_stage_outcomes_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `PoolService.stage_outcomes`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. The pool characterization test
  now calls `pool_service_for_workspace(workspace).stage_outcomes(task.id)`
  directly. Verified no matches for `rg -n
  "task_stage_outcomes_for_workspace\\(" litehive tests tests_integration`,
  then ran focused pool tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW45. Wrapper `_pool_task_report_entry_for_workspace` delegates to
  `PoolService.task_report_entry`.

  Delete when:
  - [x] `rg -n "_pool_task_report_entry_for_workspace\\(" litehive tests`
    shows only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `PoolService.task_report_entry`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. It had no production or test
  callers; pool task report entry creation is owned by
  `PoolService.task_report_entry`. Verified no matches for `rg -n
  "_pool_task_report_entry_for_workspace\\(" litehive tests tests_integration`,
  then ran focused pool tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW46. Wrapper `_pending_pool_tasks_for_workspace` delegates to
  `PoolService.collect_pending`.

  Delete when:
  - [x] `rg -n "_pending_pool_tasks_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `PoolService.collect_pending`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. It had no production or test
  callers; pending pool collection is owned by `PoolService.collect_pending`.
  Verified no matches for `rg -n "_pending_pool_tasks_for_workspace\\("
  litehive tests tests_integration`, then ran focused pool tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW47. Wrapper `_resumable_pool_tasks_for_workspace` delegates to
  `PoolService.collect_resumable`.

  Delete when:
  - [x] `rg -n "_resumable_pool_tasks_for_workspace\\(" litehive tests`
    shows only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `PoolService.collect_resumable`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. It had no production or test
  callers; resumable pool collection is owned by
  `PoolService.collect_resumable`. Verified no matches for `rg -n
  "_resumable_pool_tasks_for_workspace\\(" litehive tests tests_integration`,
  then ran focused pool tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW48. Wrapper `_closed_pool_tasks_for_workspace` delegates to
  `PoolService.collect_closed`.

  Delete when:
  - [x] `rg -n "_closed_pool_tasks_for_workspace\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `PoolService.collect_closed`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. It had no production or test
  callers; closed pool collection is owned by `PoolService.collect_closed`.
  Verified no matches for `rg -n "_closed_pool_tasks_for_workspace\\("
  litehive tests tests_integration`, then ran focused pool tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW49. Wrapper `_print_pool_summary_report` delegates to
  `PoolService.print_summary_report`.

  Delete when:
  - [x] `rg -n "_print_pool_summary_report\\(" litehive tests` shows only
    the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `PoolService.print_summary_report`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. It had no production or test
  callers; pool summary printing is owned by `PoolService.print_summary_report`.
  Verified no matches for `rg -n "_print_pool_summary_report\\(" litehive
  tests tests_integration`, then ran focused pool tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW50. Wrapper `_pool_summary_report_data_for_workspace` delegates to
  `PoolService.summarize`.

  Delete when:
  - [x] `rg -n "_pool_summary_report_data_for_workspace\\(" litehive tests`
    shows only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `PoolService.summarize`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. It had no production or test
  callers; pool summary data construction is owned by `PoolService.summarize`.
  Verified no matches for `rg -n "_pool_summary_report_data_for_workspace\\("
  litehive tests tests_integration`, then ran focused pool tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW51. Wrapper `_write_pool_summary_report` delegates to
  `PoolService.write_summary`.

  Delete when:
  - [x] `rg -n "_write_pool_summary_report\\(" litehive tests` shows only
    the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `PoolService.write_summary`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper. The pool summary write
  characterization test now calls `pool_service_for_workspace(workspace).write_summary(...)`
  directly. Verified no matches for `rg -n "_write_pool_summary_report\\("
  litehive tests tests_integration`, then ran focused pool tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW52. Wrapper `insert_recovery_report` delegates to
  `TaskReportStore.insert_recovery_report`.

  Delete when:
  - [x] `rg -n "insert_recovery_report\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskReportStore.insert_recovery_report`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/report_storage.py`. Recovery report recording already used
  `task_report_store_for_workspace(workspace).insert_recovery_report(...)`
  directly; remaining matches are the store method and private event-log
  rebuild helper. Verified no wrapper matches with `rg -n
  "insert_recovery_report\\(" litehive tests tests_integration`, then ran
  focused recovery/event-log tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW53. Wrapper `load_recovery_reports` delegates to
  `TaskReportStore.load_recovery_reports`.

  Delete when:
  - [x] `rg -n "load_recovery_reports\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskReportStore.load_recovery_reports`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/report_storage.py`. It had no production or test callers;
  remaining matches are the store method and its internal use by
  `TaskReportStore.latest_recovery_report`. Verified with `rg -n
  "load_recovery_reports\\(" litehive tests tests_integration`, then ran
  focused recovery/report tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW54. Wrapper `latest_recovery_report` delegates to
  `TaskReportStore.latest_recovery_report`.

  Delete when:
  - [x] `rg -n "latest_recovery_report\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskReportStore.latest_recovery_report`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/report_storage.py`. The pipeline CLI now calls
  `task_report_store_for_workspace(workspace).latest_recovery_report(task)`
  directly. Verified with `rg -n "latest_recovery_report\\(" litehive tests
  tests_integration`, then ran focused report/recovery tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW55. Wrapper `record_stage_report` delegates to
  `TaskReportStore.record_stage_report`.

  Delete when:
  - [x] `rg -n "record_stage_report\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskReportStore.record_stage_report`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/report_storage.py`. Production was already using
  `task_report_store_for_workspace(...).record_stage_report(...)`; remaining
  tests now call the store method directly. Verified with `rg -n
  "record_stage_report\\(" litehive tests tests_integration`, then ran focused
  report consumer tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW56. Wrapper `rewrite_latest_stage_report` delegates to
  `TaskReportStore.rewrite_latest_stage_report`.

  Delete when:
  - [x] `rg -n "rewrite_latest_stage_report\\(" litehive tests` shows only
    the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskReportStore.rewrite_latest_stage_report`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/report_storage.py`. The hallucinated-completion rewrite path
  now calls
  `task_report_store_for_workspace(workspace).rewrite_latest_stage_report(...)`
  directly. Verified with `rg -n "rewrite_latest_stage_report\\(" litehive
  tests tests_integration`, then ran focused engine/report tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW57. Wrapper `load_stage_reports_for_task_id` delegates to
  `TaskReportStore.load_stage_reports_for_task_id`.

  Delete when:
  - [x] `rg -n "load_stage_reports_for_task_id\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskReportStore.load_stage_reports_for_task_id`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/report_storage.py`. Pool reporting already calls
  `task_report_store_for_workspace(...).load_stage_reports_for_task_id(...)`
  directly. Verified with `rg -n "load_stage_reports_for_task_id\\(" litehive
  tests tests_integration`, then ran focused pool/report tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW58. Wrapper `load_workspace_stage_reports` delegates to
  `TaskReportStore.load_workspace_stage_reports`.

  Delete when:
  - [x] `rg -n "load_workspace_stage_reports\\(" litehive tests` shows only
    the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskReportStore.load_workspace_stage_reports`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/report_storage.py`. Status summary now calls
  `task_report_store_for_workspace(workspace).load_workspace_stage_reports()`
  directly. Verified with `rg -n "load_workspace_stage_reports\\(" litehive
  tests tests_integration`, then ran focused observability/status tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW59. Wrapper `load_stage_reports` delegates to
  `TaskReportStore.load_stage_reports`.

  Delete when:
  - [x] `rg -n "load_stage_reports\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskReportStore.load_stage_reports`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/report_storage.py`. Remaining tests now call
  `task_report_store_for_workspace(...).load_stage_reports(...)` directly;
  production was already on store methods. Verified with `rg -n
  "load_stage_reports\\(" litehive tests tests_integration`, then ran focused
  report/lifecycle tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW60. Wrapper `latest_stage_report` delegates to
  `TaskReportStore.latest_stage_report`.

  Delete when:
  - [x] `rg -n "latest_stage_report\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskReportStore.latest_stage_report`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/report_storage.py`. Status summary, workspace repair,
  pipeline journal, task debug, and recovery evidence now call
  `task_report_store_for_workspace(workspace).latest_stage_report(...)`
  directly. Verified with `rg -n "latest_stage_report\\(" litehive tests
  tests_integration`; remaining matches are the store method, direct store
  calls, and unrelated rewrite helpers. Ran focused CLI/status/repair tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW61. Wrapper `Workspace.task_activity` delegates to
  `TaskActivityStore`.

  Delete when:
  - [x] `rg -n "\\.task_activity\\(" litehive tests` shows no matches.
  - [x] Production callers use `task_activity_store_for_task`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted `Workspace.task_activity` and migrated
  production/test callers to `task_activity_store_for_task(...)`.
  Verified with `rg -n "\\.task_activity\\(" litehive tests`, focused
  activity/report/lifecycle tests, `make typecheck`, and `make test`.

- [x] TW62. Wrapper `TaskActivityLog` aliases `TaskActivityStore`.

  Delete when:
  - [x] `rg -n "TaskActivityLog" litehive tests` shows only the alias
    definition or no matches.
  - [x] Tests and production callers patch/use `TaskActivityStore`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the `TaskActivityLog` compatibility alias.
  `task_activity_store_for_task(...)` now constructs `TaskActivityStore`
  directly, and the prompt serializer test monkeypatches
  `TaskActivityStore.load`. Verified `rg -n "TaskActivityLog" litehive tests
  tests_integration` has no matches, then ran focused activity/prompt tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW63. Wrapper `load_task_activity` delegates to
  `TaskActivityStore.load`.

  Delete when:
  - [x] `rg -n "load_task_activity\\(" litehive tests` shows only the wrapper
    definition and intentional compatibility tests.
  - [x] Production callers use `TaskActivityStore.load`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/activity.py`. Integration helper/test callers now use
  `task_activity_store_for_task(workspace, task).load()` directly. Verified
  `rg -n "load_task_activity\\(" litehive tests tests_integration` has no
  matches, then ran `uv run pytest tests/tasks/test_activity.py -q`,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`). Real-engine
  integration tests were not run in this slice.

- [x] TW64. Wrapper `save_task_activity` delegates to
  `TaskActivityStore.save`.

  Delete when:
  - [x] `rg -n "save_task_activity\\(" litehive tests` shows only the wrapper
    definition and intentional compatibility tests.
  - [x] Production callers use `TaskActivityStore.save`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the unreferenced free-function wrapper from
  `litehive/tasks/activity.py`. Verified `rg -n "save_task_activity\\("
  litehive tests tests_integration` has no matches, then ran
  `uv run pytest tests/tasks/test_activity.py -q`, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW65. Wrapper `append_task_activity` delegates to
  `TaskActivityStore.append`.

  Delete when:
  - [x] `rg -n "append_task_activity\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskActivityStore.append`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the unreferenced free-function wrapper from
  `litehive/tasks/activity.py`. Production and tests already append through
  `task_activity_store_for_task(...).append(...)`. Verified `rg -n
  "append_task_activity\\(" litehive tests tests_integration` has no matches,
  then ran focused activity/reporting tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW66. Wrapper `latest_task_activity_entry` delegates to
  `TaskActivityStore.latest_entry`.

  Delete when:
  - [x] `rg -n "latest_task_activity_entry\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskActivityStore.latest_entry`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the unreferenced free-function wrapper from
  `litehive/tasks/activity.py`. Production and tests already use
  `task_activity_store_for_task(...).latest_entry(...)` directly. Verified
  `rg -n "latest_task_activity_entry\\(" litehive tests tests_integration`
  has no matches, then ran focused activity/agent/lifecycle tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW67. Wrapper `task_event_log_path` delegates to
  `TaskEventLog.path`.

  Delete when:
  - [x] `rg -n "task_event_log_path\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskEventLog.path`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/event_log.py`. Event-log rebuild and migration tests now
  use `task_event_log_for_workspace(workspace).path()` directly; production
  had no wrapper callers. Verified `rg -n "task_event_log_path\\(" litehive
  tests tests_integration` has no matches, then ran focused workspace/event
  log tests, `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW68. Wrapper `append_task_event` delegates to
  `TaskEventLog.append`.

  Delete when:
  - [x] `rg -n "append_task_event\\(" litehive tests` shows only the wrapper
    definition and intentional compatibility tests.
  - [x] Production callers use `TaskEventLog.append`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/event_log.py`. State, activity, audit, report storage, and
  lifecycle persistence/journal callers now use
  `task_event_log_for_workspace(...).append(...)` directly. Verified `rg -n
  "append_task_event\\(" litehive tests tests_integration` has no matches,
  then ran focused event-log/activity/audit/lifecycle/reporting tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW69. Wrapper `read_task_events` delegates to
  `TaskEventLog.read`.

  Delete when:
  - [x] `rg -n "read_task_events\\(" litehive tests` shows only the wrapper
    definition and intentional compatibility tests.
  - [x] Production callers use `TaskEventLog.read`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/event_log.py`. Event-log rebuild tests now call
  `task_event_log_for_workspace(workspace).read()` directly; production had
  no wrapper callers. Verified `rg -n "read_task_events\\(" litehive tests
  tests_integration` has no matches, then ran
  `uv run pytest tests/tasks/test_event_log_rebuild.py -q`, `make typecheck`,
  and `make test` (`940 passed, 1 skipped`).

- [x] TW70. Wrapper `task_event_log_has_events` delegates to
  `TaskEventLog.has_events`.

  Delete when:
  - [x] `rg -n "task_event_log_has_events\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskEventLog.has_events`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/event_log.py`. Bootstrap detection now calls
  `task_event_log_for_workspace(workspace).has_events()` directly, and the
  event-log test does the same. Verified `rg -n
  "task_event_log_has_events\\(" litehive tests tests_integration` has no
  matches, then ran focused rebuild/bootstrap/state tests, `make typecheck`,
  and `make test` (`940 passed, 1 skipped`).

- [x] TW71. Wrapper `rebuild_sqlite_from_task_event_log` delegates to
  `TaskEventLog.rebuild_sqlite`.

  Delete when:
  - [x] `rg -n "rebuild_sqlite_from_task_event_log\\(" litehive tests` shows
    only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskEventLog.rebuild_sqlite`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/event_log.py`. Runtime store bootstrap and the DB recovery
  CLI now call `task_event_log_for_workspace(...).rebuild_sqlite()` directly;
  event-log rebuild tests and the runtime-store monkeypatch use
  `TaskEventLog.rebuild_sqlite`. Verified `rg -n
  "rebuild_sqlite_from_task_event_log\\(" litehive tests tests_integration`
  has no matches, then ran focused event-log/runtime/CLI tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW72. Wrapper `sqlite_task_tables_empty` delegates to
  `TaskEventLog.sqlite_task_tables_empty`.

  Delete when:
  - [x] `rg -n "sqlite_task_tables_empty\\(" litehive tests` shows only the
    wrapper definition and intentional compatibility tests.
  - [x] Production callers use `TaskEventLog.sqlite_task_tables_empty`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the free-function wrapper from
  `litehive/tasks/event_log.py`. Bootstrap detection now reuses a
  `TaskEventLog` and calls `.sqlite_task_tables_empty()` directly. Verified
  `rg -n "sqlite_task_tables_empty\\(" litehive tests tests_integration`;
  remaining matches are the method definition and direct method call. Ran
  focused rebuild/runtime/bootstrap tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW73. Wrapper `collect_status_snapshot_for_workspace` delegates to
  `StatusSnapshotCollector.collect`.

  Delete when:
  - [x] `rg -n "collect_status_snapshot_for_workspace\\(" litehive tests`
    shows only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `StatusSnapshotCollector.collect`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper and removed it from
  `status_diagnostics.__all__`. The remaining test caller now uses
  `status_snapshot_collector_for_workspace(...).collect()` directly;
  production was already on the collector. Verified `rg -n
  "collect_status_snapshot_for_workspace\\(" litehive tests
  tests_integration` has no matches, then ran focused observability tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW74. Wrapper `collect_operational_status_snapshot_for_workspace`
  delegates to `StatusSnapshotCollector.collect_operational`.

  Delete when:
  - [x] `rg -n "collect_operational_status_snapshot_for_workspace\\(" litehive tests`
    shows only the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `StatusSnapshotCollector.collect_operational`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper and removed it from
  `status_diagnostics.__all__`. The remaining test caller now uses
  `status_snapshot_collector_for_workspace(...).collect_operational()`
  directly; production was already on the collector. Verified `rg -n
  "collect_operational_status_snapshot_for_workspace\\(" litehive tests
  tests_integration` has no matches, then ran focused observability tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW75. Wrapper `select_engine_for_workspace` delegates to
  `EngineRoutingPolicy.select`.

  Delete when:
  - [x] `rg -n "select_engine_for_workspace\\(" litehive tests` shows only
    the wrapper definition and intentional compatibility tests.
  - [x] Production callers use `EngineRoutingPolicy.select`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the wrapper from
  `litehive/config/engine_models.py`. Tests now call
  `engine_routing_policy_for_workspace(...).select(...)` directly, and the
  package doc note names the policy factory instead of the removed wrapper.
  Production was already on `EngineRoutingPolicy.select`. Verified `rg -n
  "select_engine_for_workspace\\(" litehive tests tests_integration` has no
  matches, then ran focused engine-routing tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW76. Facade method `WorktreeService.inspect_task_worktree` delegates
  to `WorktreeInspector.inspect_task_worktree`.

  Delete when:
  - [x] `rg -n "inspect_task_worktree\\(" litehive tests` shows only the
    inspector method and intentional compatibility tests.
  - [x] Production callers use `WorktreeInspector.inspect_task_worktree`.
  - [x] Focused tests and `make test` pass after deletion.

- [x] TW77. Facade method `WorktreeService.sync_task_worktree` delegates to
  `WorktreeSyncService.sync_task_worktree`.

  Delete when:
  - [x] `rg -n "sync_task_worktree\\(" litehive tests` shows only the sync
    service method and intentional compatibility tests.
  - [x] Production callers use `WorktreeSyncService.sync_task_worktree`.
  - [x] Focused tests and `make test` pass after deletion.

- [x] TW78. Facade method `WorktreeService.collect_managed_worktrees`
  delegates to `WorktreeCleanupService.collect_managed_worktrees`.

  Delete when:
  - [x] `rg -n "collect_managed_worktrees\\(" litehive tests` shows only
    the cleanup service method and intentional compatibility tests.
  - [x] Production callers use `WorktreeCleanupService.collect_managed_worktrees`.
  - [x] Focused tests and `make test` pass after deletion.

- [x] TW79. Facade method `WorktreeService.remove_cleanable_worktrees`
  delegates to `WorktreeCleanupService.remove_cleanable_worktrees`.

  Delete when:
  - [x] `rg -n "remove_cleanable_worktrees\\(" litehive tests` shows only
    the cleanup service method and intentional compatibility tests.
  - [x] Production callers use `WorktreeCleanupService.remove_cleanable_worktrees`.
  - [x] Focused tests and `make test` pass after deletion.

- [x] TW80. Facade method `WorktreeService.cleanup_terminal_task_worktree`
  delegates to `WorktreeCleanupService.cleanup_terminal_task_worktree`.

  Delete when:
  - [x] `rg -n "cleanup_terminal_task_worktree\\(" litehive tests` shows
    only the cleanup service method and intentional compatibility tests.
  - [x] Production callers use `WorktreeCleanupService.cleanup_terminal_task_worktree`.
  - [x] Focused tests and `make test` pass after deletion.

- [x] TW81. Facade method `WorktreeService.collect_rescue_candidates`
  delegates to `WorktreeRescueService.collect_rescue_candidates`.

  Delete when:
  - [x] `rg -n "collect_rescue_candidates\\(" litehive tests` shows only
    the rescue service method and intentional compatibility tests.
  - [x] Production callers use `WorktreeRescueService.collect_rescue_candidates`.
  - [x] Focused tests and `make test` pass after deletion.

- [x] TW82. Facade method `WorktreeService.apply_rescue_candidate`
  delegates to `WorktreeRescueService.apply_rescue_candidate`.

  Delete when:
  - [x] `rg -n "apply_rescue_candidate\\(" litehive tests` shows only the
    rescue service method and intentional compatibility tests.
  - [x] Production callers use `WorktreeRescueService.apply_rescue_candidate`.
  - [x] Focused tests and `make test` pass after deletion.

- [x] TW83. Facade method `WorktreeService.require_clean_main_checkout`
  delegates to `WorktreeRescueService.require_clean_main_checkout`.

  Delete when:
  - [x] `rg -n "require_clean_main_checkout\\(" litehive tests` shows only
    the rescue service method and intentional compatibility tests.
  - [x] Production callers use `WorktreeRescueService.require_clean_main_checkout`.
  - [x] Focused tests and `make test` pass after deletion.

- [x] TW84. Compatibility alias `DaemonExecutor` points to `WorkspaceDaemon`.

  Delete when:
  - [x] `rg -n "DaemonExecutor" litehive tests` shows only the alias or no
    matches.
  - [x] Production callers use `WorkspaceDaemon`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the `DaemonExecutor = WorkspaceDaemon`
  compatibility alias from `litehive/daemon/execution.py`. Verified `rg -n
  "DaemonExecutor" litehive tests tests_integration` has no matches, then ran
  focused daemon tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW85. Wrapper `run_once` delegates to `DaemonExecution.run_once`.

  Delete when:
  - [x] `rg -n "run_once\\(" litehive tests` shows only CLI boundary callers
    that can construct `DaemonExecution` directly.
  - [x] Production callers use `DaemonExecution.run_once`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted `litehive.cli.runner.run_once`. The single-run
  CLI path, drain pool callback, and hook-reject regression test now construct
  `DaemonExecution(...).run_once()` directly. Verified `rg -n "run_once\\("
  litehive tests tests_integration`; remaining matches are direct
  `DaemonExecution.run_once()` calls, the pool callback method name, and the
  daemon method definition. Ran focused CLI/daemon/lifecycle tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW86. Execution-trace free functions delegate to
  `ExecutionTraceRenderer` methods.

  Delete when:
  - [x] `rg -n "parse_unified_events\\(|render_event_for_execution_trace\\(|render_execution_trace_from_events\\(|render_execution_trace\\(|recovered_timeline_from_events\\(|render_execution_trace_from_streams\\(|render_execution_trace_from_event_stream_payload\\(|load_subagent_execution_trace\\(" litehive tests tests_integration`
    shows only `ExecutionTraceRenderer` method calls or unrelated Heru API
    calls.
  - [x] Production callers use `ExecutionTraceRenderer` or
    `execution_trace_renderer()`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the execution-trace free-function wrappers
  from `litehive/agents/execution_trace.py`. Tests now exercise
  `execution_trace_renderer()` directly. Production was already using
  `ExecutionTraceRenderer` through `SubagentSessionManager` and session
  orchestration. Verified with the exact `rg` command above; remaining hits
  are renderer methods, `SubagentSessionManager.render_execution_trace`, and
  Heru's `render_execution_trace` API in tests. Ran focused execution-trace
  tests, `make typecheck`, and `make test` (`940 passed, 1 skipped`).

  Continuation verification 2026-05-08: audited P3 again against the exact
  current code path. Deleted the remaining private `_read_stream_artifact`
  delegating wrapper from `litehive/agents/execution_trace.py`; the stream
  artifact selection now exists only as `ExecutionTraceRenderer._read_stream_artifact`
  and its two internal class call sites. Verified with
  `rg -n "def _read_stream_artifact|_read_stream_artifact\\(" litehive/agents/execution_trace.py litehive tests tests_integration`
  and the wrapper-free execution-trace grep above. Ran
  `uv run pytest tests/agents/test_execution_trace.py tests/agents/test_unified_events.py tests/agents/test_continuation_delegation.py tests/lifecycle/test_engine_adapter.py -q`
  (`65 passed`).

- [x] TW87. Wrapper `load_subagent_artifacts` delegates to
  `SubagentArtifactStore.load_all`.

  Delete when:
  - [x] `rg -n "load_subagent_artifacts\\(" litehive tests tests_integration`
    shows no production or test callers.
  - [x] Production callers use `subagent_artifacts(...).load_all()`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the unreferenced `load_subagent_artifacts`
  wrapper from `litehive/agents/session_store.py`. Verified `rg -n
  "load_subagent_artifacts\\(" litehive tests tests_integration` has no
  matches, then ran focused session/subagent tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW88. Wrapper `load_subagent_session_record` delegates to
  `SubagentArtifactStore.load_session_record`.

  Delete when:
  - [x] `rg -n "load_subagent_session_record\\(" litehive tests tests_integration`
    shows no production or test callers.
  - [x] Production callers use `subagent_artifacts(...).load_session_record()`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the `load_subagent_session_record` wrapper
  from `litehive/agents/session_store.py`. The remaining tests now call
  `subagent_artifacts(...).load_session_record()` directly; the
  `load_subagent_session` wrapper was deleted during TW89.
  Verified `rg -n "load_subagent_session_record\\(" litehive tests
  tests_integration` has no matches, then ran focused session/log/debug tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW89. Wrapper `load_subagent_session` delegates to
  `SubagentArtifactStore.load_session_record().values`.

  Delete when:
  - [x] `rg -n "load_subagent_session\\(" litehive tests tests_integration`
    shows no production or test callers.
  - [x] Production callers use `subagent_artifacts(...).load_session_record()`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the `load_subagent_session` wrapper from
  `litehive/agents/session_store.py`. The remaining test now reads
  `subagent_artifacts(...).load_session_record().values` directly. Verified
  `rg -n "load_subagent_session\\(" litehive tests tests_integration` has no
  matches, then ran focused session/log/debug tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW90. Wrapper `load_subagent_report` delegates to
  `SubagentArtifactStore.load_report`.

  Delete when:
  - [x] `rg -n "load_subagent_report\\(" litehive tests tests_integration`
    shows no production or test callers.
  - [x] Production callers use `subagent_artifacts(...).load_report()`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the `load_subagent_report` wrapper from
  `litehive/agents/session_store.py`. Tests now call
  `subagent_artifacts(...).load_report()` directly. Verified `rg -n
  "load_subagent_report\\(" litehive tests tests_integration` has no matches,
  then ran focused session/recovery/migration tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW91. Wrapper `load_subagent_event_stream` delegates to
  `SubagentArtifactStore.load_event_stream`.

  Delete when:
  - [x] `rg -n "load_subagent_event_stream\\(" litehive tests tests_integration`
    shows no production or test callers.
  - [x] Production callers use `subagent_artifacts(...).load_event_stream()`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the `load_subagent_event_stream` wrapper and
  the now-unused private slice helper from `litehive/agents/session_store.py`.
  Tests now call `subagent_artifacts(...).load_event_stream()` directly.
  Verified `rg -n "load_subagent_event_stream\\(" litehive tests
  tests_integration` has no matches, then ran focused session/subagent tests,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] TW92. Wrapper `stage_report_from_subagent` delegates to
  `AgentReportService.stage_report_from_subagent`.

  Delete when:
  - [x] `rg -n "stage_report_from_subagent\\(" litehive tests tests_integration`
    shows only `AgentReportService` method calls.
  - [x] Production callers use `AgentReportService.stage_report_from_subagent`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the `stage_report_from_subagent` wrapper from
  `litehive/agents/report_extraction.py`. Production was already constructing
  `AgentReportService` directly in `SubagentManager`; the remaining tests now
  instantiate `AgentReportService` directly. Updated domain/doc references to
  name the service method. Verified with the exact `rg` command above, then
  ran focused stage-report tests, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] TW93. Wrapper `_load_stage_reports` delegates to
  `TaskReportStore._load_stage_reports`.

  Delete when:
  - [x] `rg -n "_load_stage_reports\\(" litehive tests tests_integration`
    shows only `TaskReportStore` internal method calls.
  - [x] Production callers use `TaskReportStore` public loading methods.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the unreferenced module-level
  `_load_stage_reports` wrapper from `litehive/tasks/report_storage.py`.
  Production and tests already route through `task_report_store_for_workspace`
  and the store's public loading methods. Verified with the exact `rg`
  command above, then ran focused report-storage users, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW94. Wrappers `write_stream_artifact`, `write_text_artifact`, and
  `remove_text_artifact` delegate to `ArtifactService`.

  Delete when:
  - [x] `rg -n "write_stream_artifact\\(|write_text_artifact\\(|remove_text_artifact\\(" litehive tests tests_integration`
    shows no wrapper callers.
  - [x] Production callers use `ArtifactService`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the unused artifact compatibility wrappers
  from `litehive/agents/artifacts.py`. Production already created
  `ArtifactService` directly in session code; the artifact tests now exercise
  the service directly too. Kept `write_text_if_changed` as a utility used by
  `ArtifactService.write_stream`. Verified with the exact `rg` command above,
  then ran focused artifact/session/subagent tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] TW95. Wrapper `Workspace.config` delegates to `Workspace.load_config`.

  Delete when:
  - [x] `rg -n "def config\\(" litehive/workspace.py` shows the method is
    deleted.
  - [x] `rg -n "workspace\\.config\\(|Workspace\\.config" litehive tests tests_integration`
    shows no `Workspace.config()` callers.
  - [x] Production callers use `Workspace.load_config`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the `Workspace.config()` compatibility method
  from `litehive/workspace.py`. Production already used
  `Workspace.load_config()`; the remaining test now asserts the cached
  `load_config()` entrypoint directly. Verified with the exact `rg` commands
  above, then ran focused workspace/config tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] RD1. `SubagentManager` used raw `workspace.root` to build task artifact
  paths instead of `Workspace.task_dir`.

  Delete when:
  - [x] `rg -n "task_dir\\(self\\.workspace\\.root|task_dir\\(workspace\\.root" litehive tests tests_integration`
    shows no production/test callers that peel `root` out of a `Workspace`.
  - [x] `SubagentManager._prepare_subagent_run(...)` uses
    `self.workspace.task_dir(task)`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: routed `SubagentManager._prepare_subagent_run(...)`
  through `self.workspace.task_dir(task)` and removed its direct
  `litehive.tasks.paths.task_dir` import. Verified with the exact `rg`
  command above, then ran focused subagent manager tests, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] RD2. `runner_lock_path(root: Path)` remained in `litehive/tasks/paths.py`
  even though production code uses `Workspace.runtime_path`.

  Delete when:
  - [x] `rg -n "runner_lock_path\\(" litehive tests tests_integration` has no
    matches.
  - [x] `tests/test_architecture_guardrails.py` no longer allowlists
    `runner_lock_path:root:Path`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted `runner_lock_path(...)` from
  `litehive/tasks/paths.py`. The only caller was a close-active test, now
  reading `workspace.runtime_path("runtime", ".runner.lock")` directly. The
  raw-root architecture guardrail allowlist no longer includes
  `runner_lock_path`. Verified with the exact `rg` command above,
  `uv run pytest tests/test_architecture_guardrails.py tests/tasks/test_close_active.py -q`,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] RD3. `task_recovery_dir(root: Path, task)` remained in
  `litehive/tasks/paths.py` with no callers.

  Delete when:
  - [x] `rg -n "task_recovery_dir\\(" litehive tests tests_integration` has
    no matches.
  - [x] `tests/test_architecture_guardrails.py` no longer allowlists
    `task_recovery_dir:root:Path`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: deleted the unreferenced `task_recovery_dir(...)`
  helper from `litehive/tasks/paths.py` and removed it from the raw-root
  architecture guardrail allowlist. Verified with the exact `rg` command
  above, `uv run pytest tests/test_architecture_guardrails.py -q`,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] RD4. Tests imported raw-root `task_dir(...)` instead of using
  `Workspace.task_dir(...)`.

  Delete when:
  - [x] `rg -n "from litehive\\.tasks\\.paths import .*task_dir" tests tests_integration`
    has no matches.
  - [x] `rg -n "task_dir\\(" tests tests_integration` shows only
    `Workspace.task_dir(...)` method calls or local variable names.
  - [x] Focused tests and `make test` pass after migration.

  Completed 2026-05-08: migrated agent, CLI, lifecycle, state, task, and
  integration tests from raw-root `task_dir(...)` to
  `Workspace.task_dir(...)`. Verified with the exact `rg` commands above,
  focused tests for the migrated files, `make typecheck`, and `make test`
  (`940 passed, 1 skipped`).

- [x] RD5. `tasks_root(root: Path)` and `task_dir(root: Path, task)` remained
  in `litehive/tasks/paths.py` after callers moved to `Workspace.task_dir`.

  Delete when:
  - [x] `rg -n "def (tasks_root|task_dir|_worktree_workspace_dir)\\(" litehive/tasks/paths.py`
    has no matches.
  - [x] `rg -n "from litehive\\.tasks\\.paths import .*task_dir|tasks_root\\(" litehive tests tests_integration`
    has no direct task-dir imports or `tasks_root(...)` callers.
  - [x] `tests/test_architecture_guardrails.py` no longer allowlists
    `tasks_root:root:Path`, `task_dir:root:Path`, or
    `_worktree_workspace_dir:root:Path`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: moved task artifact directory resolution into
  `Workspace.task_dir(...)`, preserving the managed-worktree redirect logic
  there. Deleted `_worktree_workspace_dir(...)`, `tasks_root(...)`, and
  `task_dir(...)` from `litehive/tasks/paths.py`, then removed those raw-root
  entries from the architecture guardrail allowlist. Verified with the exact
  `rg` commands above, `uv run pytest tests/test_architecture_guardrails.py
  tests/agents/test_subagent_manager.py tests/state/test_task_cleanup.py -q`,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] RD6. `GitCommitNode._filter_stageable_paths(...)` accepted
  `repo_root: Path` even though it only filters main-checkout paths.

  Delete when:
  - [x] `rg -n "def _filter_stageable_paths\\([^)]*repo_root" litehive/lifecycle/nodes/system.py`
    has no matches.
  - [x] `tests/test_architecture_guardrails.py` no longer allowlists
    `GitCommitNode._filter_stageable_paths:repo_root:Path`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: changed `_filter_stageable_paths(...)` to use
  `self.workspace.root` directly and removed the `repo_root` parameter from
  the call site in `autocommit_main_checkout_changes(...)`. Tightened the
  architecture guardrail allowlist accordingly. Verified with the exact `rg`
  command above, `uv run pytest tests/test_architecture_guardrails.py
  tests/lifecycle/test_hooks_and_commit.py -q`, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] RD7. `_is_ignored_even_if_tracked(...)` accepted `repo_root: Path` even
  though it is only used by `GitCommitNode` main-checkout cleanup.

  Delete when:
  - [x] `rg -n "def _is_ignored_even_if_tracked\\([^)]*repo_root|_is_ignored_even_if_tracked\\([^)]*self\\.workspace\\.root" litehive/lifecycle/nodes/system.py`
    has no matches.
  - [x] `tests/test_architecture_guardrails.py` no longer allowlists
    `_is_ignored_even_if_tracked:repo_root:Path`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: moved `_is_ignored_even_if_tracked(...)` onto
  `GitCommitNode` as an instance method that uses `self.workspace.root`, then
  routed `autocommit_main_checkout_changes(...)` through that method. Tightened
  the architecture guardrail allowlist accordingly. Verified with the exact
  `rg` command above, `uv run pytest tests/test_architecture_guardrails.py
  tests/lifecycle/test_hooks_and_commit.py -q`, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] RD8. `_is_untracked_embedded_git_repo(...)` accepted `repo_root: Path`
  even though it is commit-node policy for task worktree checkpoint commits.

  Delete when:
  - [x] `rg -n "def _is_untracked_embedded_git_repo\\([^)]*repo_root|and not _is_untracked_embedded_git_repo\\(" litehive/lifecycle/nodes/system.py`
    has no matches for the old raw-root helper or old free-function call.
  - [x] `tests/test_architecture_guardrails.py` no longer allowlists
    `_is_untracked_embedded_git_repo:repo_root:Path`.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: moved `_is_untracked_embedded_git_repo(...)` onto
  `GitCommitNode` as an instance method. It still accepts the task `worktree`
  path because the policy checks a non-workspace checkout, but no longer
  exposes a generic raw-root module helper. Tightened the architecture
  guardrail allowlist accordingly. Verified with the exact `rg` command above,
  `uv run pytest tests/test_architecture_guardrails.py
  tests/lifecycle/test_hooks_and_commit.py
  tests/lifecycle/test_persisted_worktree_path.py -q`, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] RD9. `GitCommitNode.git_status_entries(...)` and
  `_git_status_entries_with_options(...)` accepted `repo_root: Path` as generic
  internal status seams.

  Delete when:
  - [x] `rg -n "def git_status_entries\\(|def _git_status_entries_with_options\\(|\"git_status_entries\"" litehive/lifecycle/nodes/system.py tests/lifecycle/test_hooks_and_commit.py`
    shows no old generic status methods or callers.
  - [x] `tests/test_architecture_guardrails.py` has an empty raw-root
    allowance list.
  - [x] Focused tests and `make test` pass after deletion.

  Completed 2026-05-08: replaced the generic raw-root status seam with
  explicit `GitCommitNode.main_checkout_git_status_entries(...)` and
  `GitCommitNode.worktree_git_status_entries(worktree, ...)` methods. The
  porcelain parser is now `_parse_git_status_lines(...)`, a pure lines-to-tuples
  helper with no path parameter. Updated the stale-pathspec test to monkeypatch
  the main-checkout seam directly, and tightened the raw-root architecture
  guardrail to an empty allowance list. Verified with the exact `rg` command
  above, `uv run pytest tests/test_architecture_guardrails.py
  tests/lifecycle/test_hooks_and_commit.py
  tests/lifecycle/test_persisted_worktree_path.py -q`, `make typecheck`, and
  `make test` (`940 passed, 1 skipped`).

- [x] RD10. P12.2 still said `DaemonExecutor` remained as a temporary
  compatibility alias even though TW84 deleted it.

  Delete when:
  - [x] `rg -n "DaemonExecutor remains|DaemonExecutor" litehive tests tests_integration docs/object-refactor-plan-2026-05-08.md`
    shows no code references and only the completed TW84/RD10 documentation.
  - [x] The P12.2 summary points readers at TW84 instead of claiming the alias
    still exists.

  Completed 2026-05-08: corrected the P12.2 summary to say the temporary
  `DaemonExecutor` alias was later deleted during TW84. Verified the exact
  `rg` command above and confirmed `litehive/daemon/execution.py` only defines
  `WorkspaceDaemon`.

- [x] RD11. The P0.2 and final-verification notes still described
  `litehive/tasks/paths.py` and `litehive/lifecycle/nodes/system.py` as
  remaining raw-root debt after RD2-RD9 cleared those paths.

  Delete when:
  - [x] `rg -n "def .*\\([^)]*(root|repo_root|workspace_root|main_repo_root): Path" litehive/tasks/paths.py litehive/lifecycle/nodes/system.py`
    has no matches.
  - [x] `rg -n "already[- ]documented .*tasks/paths\\.py|remains as [a] temporary compatibility alias" docs/object-refactor-plan-2026-05-08.md`
    has no matches.
  - [x] `rg -n "the rest still take" litehive/workspace.py`
    has no matches.
  - [x] `tests/test_architecture_guardrails.py` keeps the internal raw-root
    allowance list empty.

  Completed 2026-05-08: updated the P0.2 note and final-verification summary
  to say the originally frozen `tasks/paths.py` and `lifecycle/nodes/system.py`
  debt was cleared by RD2-RD9. Also updated the `Workspace` module docstring so
  it no longer says unported feature areas still take `root: Path`; it now
  distinguishes workspace-bound feature code from explicit filesystem
  boundaries. Verified with the exact `rg` commands above and the architecture
  guardrail.

- [x] RD12. The final-verification checklist still allowed raw-root hits as
  stale documented debt after the internal raw-root allowances had been cleared.

  Delete when:
  - [x] `rg -n "or listed d[e]bt" docs/object-refactor-plan-2026-05-08.md`
    has no matches.
  - [x] `rg -n "listed d[e]bt" docs/object-refactor-plan-2026-05-08.md`
    has no matches.
  - [x] `rg -n "def .*\\([^)]*(root|repo_root|workspace_root|main_repo_root): Path" litehive/tasks/paths.py litehive/lifecycle/nodes/system.py`
    has no matches.
  - [x] `tests/test_architecture_guardrails.py` keeps the internal raw-root
    allowance list empty.

  Completed 2026-05-08: tightened the final-verification checklist to say the
  remaining raw-root hits are explicit filesystem boundaries, not open-ended
  stale debt allowances. Verified with the exact `rg` commands above and the
  architecture guardrail.

- [x] RD13. Phase 1 still described runtime-settings tests and caller routing
  in terms of old free-function wrappers after TW1-TW5 and P1.4 deleted those
  wrappers.

  Delete when:
  - [x] `rg -n "bootstrap_runtime_settings\\(|load_runtime_settings\\(|apply_runtime_settings_to_config_data\\(|set_runtime_setting\\(|load_runtime_setting_audit_entries\\(" litehive tests tests_integration`
    has no matches.
  - [x] `rg -n "Existing tests still pass through old free-function wrapper[s]|Remaining matches are wrappers, tests, or intentionally unmigrated caller[s]" docs/object-refactor-plan-2026-05-08.md`
    has no matches.
  - [x] `tests/config/test_runtime_settings.py` imports and exercises
    `runtime_settings_repository_for_workspace(...)`.

  Completed 2026-05-08: updated the P1.1-P1.3 text so it describes direct
  `RuntimeSettingsRepository` coverage and the current no-match wrapper state
  instead of the temporary wrapper state that existed before P1.4. Verified
  with the exact `rg` commands above and the focused runtime-settings tests.

- [x] RD14. Phase 4 still described runtime-transition verification as old
  free-function wrappers delegating to `TaskRuntimeTransitions` after TW16-TW28
  deleted those wrappers.

  Delete when:
  - [x] `rg -n "mark_task_run_started_for_workspace\\(|mark_task_run_finished_for_workspace\\(|finish_task_run_transition_for_workspace\\(|set_task_retry_state_for_workspace\\(|clear_task_outcome_for_workspace\\(|mark_task_outcome_for_workspace\\(|mark_stage_started_for_workspace\\(|mark_stage_finished_for_workspace\\(|mark_engine_switch_for_workspace\\(" litehive tests tests_integration`
    has no matches.
  - [x] `rg -n "Existing free function[s] delegate to the new object" docs/object-refactor-plan-2026-05-08.md`
    has no matches.
  - [x] `tests/tasks/test_runtime_updates.py` exercises
    `task_runtime_transitions_for_workspace(...)` directly.

  Completed 2026-05-08: updated the P4.2 text so it describes the current
  direct `TaskRuntimeTransitions` coverage and no-match wrapper state instead
  of the temporary wrapper state that existed before TW16-TW28 cleanup.
  Verified with the exact `rg` commands above and focused runtime-transition
  tests.

- [x] RD15. Phase 5.2 still showed the original target `TaskQueueService`
  method list instead of the current service API after queue wrapper cleanup.

  Delete when:
  - [x] `rg -n "class TaskQueueService|def (eligible_tasks|select_next|peek_next_selection|dequeue_next|peek_next|enqueue|move|prioritize|remove|mark_active|clear_active|restore_untouched_active|active_task_markers|validate_single_active_task|is_resumable|is_runnable|remove_from_state|restore_missing_from_state)\\(" litehive/tasks/queue.py`
    shows the current service methods.
  - [x] `rg -n "restore_missing_queued_tasks\\(\\)" docs/object-refactor-plan-2026-05-08.md`
    has no matches.
  - [x] Public wrapper-name search against `litehive/tasks/queue.py` has no
    matches for the old queue wrappers.

  Completed 2026-05-08: updated the P5.2 method list and verification note to
  match the current `TaskQueueService` API. The old public queue wrappers are
  deleted from `litehive/tasks/queue.py`; lower-level implementation functions
  still live behind the service in `queue_mutations.py` and
  `queue_selection.py`. Verified with the exact `rg` commands above and
  focused queue tests.

- [x] RD16. Phase 5.3 still showed the original target `PoolService` method
  list instead of the current reporting and summary API.

  Delete when:
  - [x] `rg -n "class PoolService|def (stage_outcomes|task_report_entry|collect_pending|collect_resumable|collect_closed|summarize|render_summary|render_summary_report|write_summary|print_summary|print_summary_report|stop_for|run)\\(" litehive/cli/pool.py`
    shows the current service methods.
  - [x] `rg -n "task_stage_outcomes_for_workspace|_pending_pool_tasks_for_workspace|_resumable_pool_tasks_for_workspace|_closed_pool_tasks_for_workspace|_print_pool_summary_report|_pool_summary_report_data_for_workspace|_write_pool_summary_report" litehive tests tests_integration`
    has no legacy helper matches.
  - [x] `tests/cli/test_pool.py` exercises `pool_service_for_workspace(...)`
    directly.

  Completed 2026-05-08: updated the P5.3 method list and verification note to
  match the current `PoolService` API, including stage-outcome, report-entry,
  summary render/write/print, stop-reason, and run-loop methods. Verified with
  the exact `rg` commands above and focused pool tests.

- [x] RD17. Phase 6.1 still used a broad report-function grep that mixed
  `TaskReportStore` storage methods with the higher-level
  `record_recovery_report(...)` application service.

  Delete when:
  - [x] `rg -n "class TaskReportStore|def (insert_recovery_report|load_recovery_reports|latest_recovery_report|record_stage_report|rewrite_latest_stage_report|load_stage_reports_for_task_id|load_workspace_stage_reports|load_stage_reports|latest_stage_report)\\(" litehive/tasks/report_storage.py`
    shows the current store methods.
  - [x] `rg -n "^def (insert_recovery_report|load_recovery_reports|latest_recovery_report|record_stage_report|rewrite_latest_stage_report|load_stage_reports_for_task_id|load_workspace_stage_reports|load_stage_reports|latest_stage_report)\\(" litehive/tasks/report_storage.py`
    has no module-level storage wrappers.
  - [x] `rg -n "def record_recovery_report\\(" litehive/tasks/recovery_reports.py`
    confirms `record_recovery_report(...)` is the application service.

  Completed 2026-05-08: tightened P6.1 to verify the actual
  `TaskReportStore` method surface and clarified that
  `record_recovery_report(...)` belongs to the recovery-report application
  service, not the old raw storage wrappers. Verified with the exact `rg`
  commands above and focused report/recovery tests.

- [x] RD18. P6.2 and the `TaskActivityStore` docstring still referenced the
  deleted `Workspace.task_activity(...)` wrapper.

  Delete when:
  - [x] `rg -n "Workspace\\.task_activity|\\.task_activity\\(|TaskActivityLog|load_task_activity\\(|save_task_activity\\(|append_task_activity\\(|latest_task_activity_entry\\(" litehive tests tests_integration`
    has no matches.
  - [x] `rg -n "workspace\\.task_activity|Workspace\\.task_activity" litehive/tasks/activity.py`
    has no matches.
  - [x] `rg -n "Workspace\\.task_activity.*temporary wrapper|Workspace\\.task_activity.*was documented" docs/object-refactor-plan-2026-05-08.md`
    has no matches.
  - [x] `rg -n "class TaskActivityStore|def (load|save|append|latest_entry|latest)\\(|task_activity_store_for_task" litehive/tasks/activity.py tests/tasks/test_activity.py`
    shows the current store methods and direct tests.

  Completed 2026-05-08: updated `TaskActivityStore`'s docstring and P6.2 so
  they point to `task_activity_store_for_task(workspace, task)` instead of the
  deleted `Workspace.task_activity(...)` wrapper. Verified with the exact `rg`
  commands above and focused activity tests.

- [x] RD19. Phase 6.3 used a broad event-log grep that mixed current
  `TaskEventLog` methods with deleted event-log wrapper names.

  Delete when:
  - [x] `rg -n "class TaskEventLog|def (path|append|read|has_events|rebuild_sqlite|sqlite_task_tables_empty)\\(|task_event_log_for_workspace" litehive/tasks/event_log.py tests/tasks/test_event_log_rebuild.py tests/config/test_workspace_bootstrap.py tests/state/test_db_migrations.py`
    shows the current event-log methods and direct tests.
  - [x] `rg -n "^def (task_event_log_path|append_task_event|read_task_events|task_event_log_has_events|rebuild_sqlite_from_task_event_log|sqlite_task_tables_empty)\\(" litehive/tasks/event_log.py`
    has no module-level wrapper matches.
  - [x] `tests/tasks/test_event_log_rebuild.py` exercises
    `task_event_log_for_workspace(...)` directly.

  Completed 2026-05-08: updated P6.3 to verify the current `TaskEventLog` API
  and the absence of deleted module-level wrapper functions. Verified with the
  exact `rg` commands above and focused event-log tests.

- [x] RD20. Phase 7 still described status snapshot collection in terms of
  deleted `collect_status_snapshot_for_workspace(...)` and
  `collect_operational_status_snapshot_for_workspace(...)` wrappers.

  Delete when:
  - [x] `rg -n "collect_status_snapshot_for_workspace|collect_operational_status_snapshot_for_workspace" litehive tests tests_integration`
    has no matches.
  - [x] `rg -n "class StatusSnapshotCollector|def (collect|collect_operational|load_config|load_state|probe_runner|probe_daemon|probe_origin_divergence|probe_recovery_failure)\\(|status_snapshot_collector_for_workspace" litehive/observability/status_diagnostics.py tests/observability/test_status_diagnostics.py tests/observability/test_task_summary.py litehive/observability/status.py`
    shows the current collector methods and direct callers.
  - [x] `rg -n "Existing status function[s] delegate|wrapper exports/definition[s]" docs/object-refactor-plan-2026-05-08.md`
    has no matches.

  Completed 2026-05-08: updated P7.2/P7.3 to describe direct
  `status_snapshot_collector_for_workspace(...)` usage and the no-match state
  for the deleted status collection wrappers. Verified with the exact `rg`
  commands above and focused status tests.

- [x] RD21. Phase 8.2 still described engine routing as config/engine free
  functions delegating to `EngineRoutingPolicy` after the
  `select_engine_for_workspace(...)` wrapper was deleted.

  Delete when:
  - [x] `rg -n "class EngineRoutingPolicy|def (select|resolve_engine_name|resolve_model_override|resolve_recovery_engine|freeze|unfreeze|set_default|set_preference|quota_status|clear_expired_freezes)\\(|engine_routing_policy_for_workspace" litehive/config/engine_models.py`
    shows the current policy methods and factory.
  - [x] `rg -n "select_engine_for_workspace\\(" litehive tests tests_integration`
    has no calls to the deleted wrapper.
  - [x] `rg -n "Config/engine free functions delegate to [`]EngineRoutingPolicy[`]" docs/object-refactor-plan-2026-05-08.md`
    has no matches.

  Completed 2026-05-08: updated P8.2 to describe the current
  `EngineRoutingPolicy` method surface, the deleted
  `select_engine_for_workspace(...)` wrapper, and the intentionally retained
  pure helper APIs. Verified with the exact `rg` commands above and focused
  engine-routing tests.

- [x] RD22. Phase 8.3 still described engine-routing grep output with a vague
  debt label after the deleted selector wrapper was gone.

  Delete when:
  - [x] `rg -n "compatibility wrappers[/]debt" docs/object-refactor-plan-2026-05-08.md`
    has no matches.
  - [x] `rg -n "select_engine_for_workspace\\(" litehive tests tests_integration`
    has no calls to the deleted wrapper.
  - [x] `rg -n "select_engine|resolve_engine_name|resolve_recovery_engine|set_engine" litehive`
    shows policy internals, retained pure helpers, runtime-setting helpers, and
    the recovery-engine facade.

  Completed 2026-05-08: tightened P8.3 to describe the actual remaining grep
  hits instead of using the old generic debt label. Verified with the
  exact `rg` commands above and focused engine-routing tests.

Template:

- TW. Wrapper `<old_function>` delegates to `<NewClass.method>`.

  Delete when:
  - `rg -n "<old_function>\\(" litehive tests` shows only the wrapper
    definition and intentional compatibility tests.
  - Production callers use `<NewClass.method>`.
  - Focused tests and `make test` pass after deletion.

## Phase 1: Runtime Settings Repository

Reason to start here: it is cohesive, bounded, and mostly contained in
`litehive/config/runtime_settings.py`.

- [x] P1.1. Add characterization tests for current runtime-settings behavior.

  Cover:
  - [x] bootstrap from defaults/global/workspace config
  - [x] load current runtime settings
  - [x] apply runtime settings over config data
  - [x] set setting writes audit entry
  - [x] malformed stored JSON is tolerated where current behavior tolerates it

  Verification:
  - [x] `uv run pytest tests/config/test_engine_freeze.py tests/config/test_loading.py -q`

  Completed 2026-05-08: added
  `tests/config/test_runtime_settings.py` with direct repository
  characterization coverage for bootstrap-once behavior, current setting load,
  database-over-config overlay, audited setting writes and no-op behavior, and
  malformed stored JSON tolerance for settings and audit rows.
  Verified with
  `uv run pytest tests/config/test_runtime_settings.py tests/config/test_engine_freeze.py tests/config/test_loading.py -q`.

- [x] P1.2. Introduce `RuntimeSettingsRepository`.

  Target package:
  - `litehive/config/runtime_settings.py`

  Constructor dependencies:
  - `workspace: Workspace`
  - config-data loader callable
  - clock callable if needed

  Methods:
  - `bootstrap(config_data=None)`
  - `load()`
  - `apply_to_config_data(config_data)`
  - `get(key)`
  - `set(key, value, actor, source, reason=None, context=None)`
  - `audit_entries(key=None, limit=...)`

  Verification:
  - [x] Existing tests cover the repository behavior directly.
  - [x] `make typecheck`

  Completed 2026-05-08: introduced `RuntimeSettingsRepository` in
  `litehive/config/runtime_settings.py` with injected `Workspace`, config-data
  loader, and clock. At this step, existing public free functions delegated to
  repository methods and were recorded as TW1-TW5 in the Temporary Wrapper
  Cleanup Ledger; those wrappers were deleted during P1.4. Removed the now-dead
  `_bootstrap_config_data` helper. Verified with
  `uv run pytest tests/config/test_runtime_settings.py tests/config/test_engine_freeze.py tests/config/test_loading.py -q`
  and `make typecheck`.

- [x] P1.3. Route production callers to `RuntimeSettingsRepository` through the
  container or a local repository construction at an existing boundary.

  Verification:
  - [x] `rg -n "load_runtime_settings\\(|set_runtime_setting\\(|bootstrap_runtime_settings\\(" litehive`
  - [x] The old wrapper-call search now returns no matches.
  - [x] `uv run pytest tests/config -q`
  - [x] `make test`

  Completed 2026-05-08: routed `litehive/config/loading.py`,
  `litehive/cli/runner.py`, `litehive/cli/engine.py`, and internal typed
  runtime-setting helpers to `runtime_settings_repository_for_workspace(...)`.
  At this step, the wrapper-call `rg` reported only wrapper definitions and
  tests; P1.4 later deleted those wrappers.
  Verified with `uv run pytest tests/config -q`, `make typecheck`, and
  `make test`.

- [x] P1.4. Remove runtime-settings wrappers once no production callers need
  them.

  Verification:
  - [x] `rg -n "load_runtime_settings\\(|set_runtime_setting\\(|bootstrap_runtime_settings\\(" litehive`
  - [x] No production wrapper-only calls remain.

  Completed 2026-05-08: deleted `bootstrap_runtime_settings`,
  `load_runtime_settings`, `apply_runtime_settings_to_config_data`,
  `set_runtime_setting`, and `load_runtime_setting_audit_entries` from
  `litehive/config/runtime_settings.py`. Updated runtime-settings and
  engine-freeze tests to use `runtime_settings_repository_for_workspace(...)`
  directly. Verified no wrapper-call matches remain with the `rg` command,
  and reran focused config tests, `make typecheck`, and `make test`.

## Phase 2: WorkspaceTasks / TaskRepository

Reason: this removes task persistence behavior from `Workspace` and starts
shrinking `litehive/state/records.py`.

- [x] P2.1. Add characterization tests for task repository behavior.

  Cover:
  - [x] create task
  - [x] list tasks with runtime
  - [x] get task
  - [x] require missing task error
  - [x] save task
  - [x] save/write/load runtime
  - [x] discard created task
  - [x] runtime gitignore refresh

  Verification:
  - [x] `uv run pytest tests/state/test_task_repository_characterization.py tests/state/test_task_persistence.py tests/state/test_task_cleanup.py tests/state/test_task_runtime_storage.py -q`
  - [x] `uv run pytest tests/state -q`

  Completed 2026-05-08: added
  `tests/state/test_task_repository_characterization.py` covering create/list,
  get/require, save, runtime write/save/load, discard-created cleanup, and
  runtime `.gitignore` refresh through the exact current free functions in
  `litehive/state/records.py`. The originally listed
  `tests/tasks/test_task_persistence.py` path does not exist, so verification
  used
  `uv run pytest tests/state/test_task_repository_characterization.py tests/state/test_task_persistence.py tests/state/test_task_cleanup.py tests/state/test_task_runtime_storage.py -q`
  and the broader `uv run pytest tests/state -q`.

- [x] P2.2. Introduce `WorkspaceTasks` or `TaskRepository`.

  Target package:
  - Prefer `litehive/tasks/repository.py` if the public concept is tasks.
  - Prefer `litehive/state/task_repository.py` if the implementation is still
    clearly persistence-only.

  Constructor dependencies:
  - `workspace: Workspace`
  - `runtime_store: RuntimeStore`
  - event/audit collaborators only when needed

  Methods:
  - `create(...)`
  - `list(include_runtime=True, strict=True)`
  - `get(task_id)`
  - `get_record(task_id)`
  - `require(task_id)`
  - `save(task)`
  - `discard_created(task_id)`
  - `save_runtime(task)`
  - `write_runtime(task)`
  - `load_runtime(task)`
  - `ensure_runtime_ignored()`
  - `next_task_id()`

  Verification:
  - [x] Runtime-transition tests call the service directly, and old wrapper
    names have no matches.
  - [x] `make typecheck`

  Completed 2026-05-08: introduced `WorkspaceTasks` in
  `litehive/state/records.py` with workspace and runtime-store dependencies,
  plus methods for task CRUD, runtime persistence, runtime loading, runtime
  ignore refresh, and task-id reservation. At this step, existing public free
  functions delegated to `workspace_tasks_for_workspace(workspace)` and were
  recorded as TW6-TW15 in the Temporary Wrapper Cleanup Ledger; those wrappers
  were later deleted. Verified the exact wrapper path with
  `rg -n "Temporary wrapper for.*WorkspaceTasks|def (workspace_tasks_for_workspace|create_task_for_workspace|_create_task_for_workspace_impl|list_tasks_for_workspace|_list_tasks_for_workspace_impl|save_task_for_workspace|_save_task_for_workspace_impl)" litehive/state/records.py`,
  then ran
  `uv run pytest tests/state/test_task_repository_characterization.py tests/state/test_task_persistence.py tests/state/test_task_cleanup.py tests/state/test_task_runtime_storage.py -q`
  and `make typecheck`.

- [x] P2.3. Add `tasks: WorkspaceTasks` to `LitehiveContainer`.

  Verification:
  - [x] Container tests cover that the same workspace is injected.
  - [x] `uv run pytest tests/cli tests/config/test_engine_freeze.py -q`

  Completed 2026-05-08: added `tasks: WorkspaceTasks` to
  `LitehiveContainer`, wired it through `build_container(...)` with
  `workspace_tasks_for_workspace(workspace)`, and added
  `tests/test_container.py` to verify that the injected task service carries
  the exact same `Workspace` object as the container. Verified with
  `uv run pytest tests/test_container.py -q`, `make typecheck`, and
  `uv run pytest tests/cli tests/config/test_engine_freeze.py -q`.

- [x] P2.4. Route callers that already have a container to `container.tasks`.

  Verification:
  - [x] `rg -n "Workspace\\.from_path|list_tasks_for_workspace|get_task_for_workspace|save_task_for_workspace" litehive/cli litehive/daemon`
  - [x] The exact command above returns no matches.

  Completed 2026-05-08: the exact verification command returned no matches, so
  there were no CLI or daemon callers in this slice to route to
  `container.tasks`.

- [x] P2.5. Move simple `Workspace` convenience methods to wrappers over
  `WorkspaceTasks`, then remove them after callers migrate.

  Methods:
  - [x] `Workspace.list_tasks`
  - [x] `Workspace.get_task`
  - [x] `Workspace.get_task_record`
  - [x] `Workspace.require_task`
  - [x] `Workspace.save_task`

  Verification:
  - [x] `rg -n "\\.list_tasks\\(|\\.get_task\\(|\\.save_task\\(" litehive tests`
  - [x] `make test`

  Completed 2026-05-08: changed each `Workspace` task convenience method in
  `litehive/workspace.py` to delegate to `workspace_tasks_for_workspace(self)`
  and the matching `WorkspaceTasks` method. At this step, the `rg`
  verification still showed compatibility callers, so deletion of these method
  shims was tracked as P2.6; P2.6 later deleted them. Verified with focused
  task/container tests, `make typecheck`, and `make test`.

- [x] P2.6. Route `Workspace` task-method callers to `WorkspaceTasks` or
  narrower services, then delete the `Workspace` method shims.

  Methods to delete after callers migrate:
  - [x] `Workspace.list_tasks`
  - [x] `Workspace.get_task`
  - [x] `Workspace.get_task_record`
  - [x] `Workspace.require_task`
  - [x] `Workspace.save_task`

  Verification:
  - [x] `rg -n "\\.list_tasks\\(|\\.get_task\\(|\\.get_task_record\\(|\\.require_task\\(|\\.save_task\\(" litehive tests`
  - [x] `rg -n "def (list_tasks|get_task|get_task_record|require_task|save_task)\\b" litehive/workspace.py`
  - [x] Focused tests for migrated packages pass.
  - [x] `make test`

  Progress 2026-05-08:
  - Migrated `litehive/cli/pool.py` from `Workspace.list_tasks()` to
    `workspace_tasks_for_workspace(workspace).list()`. Verified with
    `rg -n "\\.list_tasks\\(|\\.get_task\\(|\\.get_task_record\\(|\\.require_task\\(|\\.save_task\\(" litehive/cli/pool.py`
    returning no matches, `uv run pytest tests/cli/test_pool.py tests/domain/test_pool_domain.py -q`,
    and `make typecheck`.
  - Migrated `litehive/cli/queue_cli.py` from `Workspace.list_tasks()`,
    `Workspace.require_task()`, and `Workspace.get_task_record()` to
    `WorkspaceTasks`. Verified with
    `rg -n "workspace_obj\\.(list_tasks|get_task|get_task_record|require_task|save_task)|workspace\\.(list_tasks|get_task|get_task_record|require_task|save_task)" litehive/cli/queue_cli.py`
    returning no matches,
    `uv run pytest tests/cli/test_entrypoint.py tests/cli/test_main_entrypoint.py tests/tasks/test_parked_lifecycle.py tests/tasks/test_flag_auto_defer.py -q`,
    and `make typecheck`.
  - Migrated `litehive/cli/task_cli.py` from `Workspace.list_tasks()`,
    `Workspace.get_task()`, and `Workspace.require_task()` plus the
    `create_task_for_workspace()` wrapper to `WorkspaceTasks`. Verified with
    `rg -n "workspace(_obj)?\\.(list_tasks|get_task|get_task_record|require_task|save_task)|create_task_for_workspace" litehive/cli/task_cli.py`
    returning no matches,
    `uv run pytest tests/cli/test_task_list_and_show.py tests/cli/test_task_debug.py tests/cli/test_task_logs_support.py tests/tasks/test_status_updates.py -q`,
    and `make typecheck`.
  - Migrated the remaining CLI modules (`litehive/cli/task_logs_support.py`,
    `litehive/cli/runner.py`, `litehive/cli/pipeline_cli.py`, and
    `litehive/cli/workspace.py`) from Workspace task methods to
    `WorkspaceTasks`. Updated the full-status characterization test to patch
    `WorkspaceTasks.list`, matching the new exact code path. Verified with
    `rg -n "\\.list_tasks\\(|\\.get_task\\(|\\.get_task_record\\(|\\.require_task\\(|\\.save_task\\(" litehive/cli`
    returning no matches, `uv run pytest tests/cli -q`, and `make typecheck`.
  - Migrated `litehive/worktree/` callers from Workspace task methods to
    `WorkspaceTasks`, including `WorktreeService`, cleanup, rescue, inspection,
    and execution-root helpers. Verified with
    `rg -n "\\.list_tasks\\(|\\.get_task\\(|\\.get_task_record\\(|\\.require_task\\(|\\.save_task\\(" litehive/worktree`
    returning no matches,
    `uv run pytest tests/tasks/test_worktrees.py tests/cli/test_worktree_clean_with_active_runner.py tests/cli/test_worktree_support.py tests/cli/test_worktree_rescue.py tests/lifecycle/test_worktree_sync.py tests/lifecycle/test_persisted_worktree_path.py -q`,
    and `make typecheck`.
  - Migrated attention, observability, recovery repair, execution recovery, and
    recovery-role prompt assembly callers to `WorkspaceTasks`. Updated the
    active-task status characterization test to patch `WorkspaceTasks.get`.
    Verified with
    `rg -n "\\.list_tasks\\(|\\.get_task\\(|\\.get_task_record\\(|\\.require_task\\(|\\.save_task\\(" litehive/attention.py litehive/observability litehive/recovery litehive/roles/recovery.py`
    returning no matches,
    `uv run pytest tests/observability tests/recovery tests/cli/test_workspace_health.py tests/daemon/test_execution.py -q`,
    and `make typecheck`.
  - Migrated `litehive/tasks/` callers from Workspace task methods to
    `WorkspaceTasks`, including queue selection/mutation, status close/resume/
    update, engine switching, stopping, completion recovery, and eligibility
    checks. Verified with
    `rg -n "workspace\\.(list_tasks|get_task|get_task_record|require_task|save_task)" litehive/tasks`
    returning no matches, `uv run pytest tests/tasks -q`, and `make typecheck`.
  - Migrated the remaining agents, lifecycle, and state-locking callers to
    `WorkspaceTasks`. Verified with the global caller grep returning no matches,
    `uv run pytest tests/agents tests/lifecycle tests/tasks/test_close_active.py tests/tasks/test_status_updates.py tests/tasks/test_zombie_queue_regressions.py tests/tasks/test_parked_lifecycle.py -q`,
    and `make typecheck`.

  Completed 2026-05-08: deleted `Workspace.list_tasks`, `Workspace.get_task`,
  `Workspace.get_task_record`, `Workspace.require_task`, and
  `Workspace.save_task` from `litehive/workspace.py`. Verified no remaining
  call sites with
  `rg -n "\\.list_tasks\\(|\\.get_task\\(|\\.get_task_record\\(|\\.require_task\\(|\\.save_task\\(" litehive tests`,
  verified the method definitions are gone with
  `rg -n "def (list_tasks|get_task|get_task_record|require_task|save_task)\\b" litehive/workspace.py`,
  then ran `make typecheck` and `make test`.

## Phase 3: ExecutionTraceRenderer

Reason: it is mostly stateless rendering/parsing and a good low-risk service
extraction.

- [x] P3.1. Add characterization tests for execution trace rendering.

  Cover:
  - [x] parse unified events
  - [x] render event
  - [x] render from events
  - [x] render from streams
  - [x] render from event-stream payload
  - [x] load subagent execution trace from artifacts/session state

  Verification:
  - [x] `uv run pytest tests/agents -q`

  Completed 2026-05-08: expanded `tests/agents/test_execution_trace.py` with
  direct characterization coverage for unified-event parsing, single-event
  tool-block rendering, rendering from parsed events, raw stream rendering,
  event-stream payload rendering, and runtime-snippet fallback when artifacts
  are missing. Existing tests already covered event-stream and cached-file load
  priority. Verified with `uv run pytest tests/agents/test_execution_trace.py -q`,
  `make typecheck`, and `uv run pytest tests/agents -q`.

- [x] P3.2. Introduce `ExecutionTraceRenderer`.

  Target package:
  - `litehive/agents/execution_trace.py`

  Methods:
  - `parse_unified_events(stdout)`
  - `render_event(event)`
  - `render_from_events(events, stderr="")`
  - `render_from_streams(stdout, stderr)`
  - `render_from_payload(payload, stderr="")`
  - `load_for_subagent(task, ref, active=None, runtime_state=None)`

  Verification:
  - [x] Existing free functions delegate to the renderer.
  - [x] `make typecheck`

  Completed 2026-05-08: introduced `ExecutionTraceRenderer` in
  `litehive/agents/execution_trace.py` with methods for parsing unified
  events, rendering single events, rendering parsed events, rendering
  CLI-execution results, rendering stream and event-stream payload fallbacks,
  recovering timelines, and loading traces for a subagent. At this step, the
  existing free functions delegated through `execution_trace_renderer()`; those
  wrappers were later deleted during TW86. Verified the wrapper path with
  `rg -n "def (parse_unified_events|render_event_for_execution_trace|render_execution_trace_from_events|render_execution_trace\\(|render_execution_trace_from_streams|render_execution_trace_from_event_stream_payload|load_subagent_execution_trace)|class ExecutionTraceRenderer|execution_trace_renderer" litehive/agents/execution_trace.py`,
  then ran focused trace/manager tests, `make typecheck`, and
  `uv run pytest tests/agents -q`.

- [x] P3.3. Route `SubagentManager` and session/report callers to the renderer.

  Verification:
  - [x] `rg -n "render_execution_trace|parse_unified_events|load_subagent_execution_trace" litehive`
  - [x] Remaining free-function calls are wrappers or tests.
  - [x] `uv run pytest tests/agents tests/lifecycle/test_engine_adapter.py -q`

  Completed 2026-05-08: routed `SubagentManager`,
  `SubagentSessionManager`, recovery prompt assembly, recovery evidence,
  interrupted-subagent recovery, and task-debug evidence loading to
  `execution_trace_renderer()`. The `rg` verification now reports only
  `ExecutionTraceRenderer` method calls in `litehive/agents/execution_trace.py`,
  `SubagentSessionManager`, and tests; no production caller imports or invokes
  the old free-function API. Verified with
  `make typecheck` and
  `uv run pytest tests/agents tests/lifecycle/test_engine_adapter.py -q`.

## Phase 4: TaskRuntimeTransitions

Reason: `litehive/tasks/runtime.py` has many `apply_*` and
`mark_*_for_workspace` pairs that are one domain concern.

- [x] P4.1. Add characterization tests for every runtime transition pair.

  Cover:
  - [x] run started/finished
  - [x] stage started/finished
  - [x] subagent started/progress/finished
  - [x] engine switch
  - [x] task outcome
  - [x] finish run transition queue behavior

  Verification:
  - [x] `uv run pytest tests/tasks/test_runtime_updates.py tests/config/test_engine_freeze.py -q`

  Completed 2026-05-08: expanded `tests/tasks/test_runtime_updates.py` with
  characterization coverage for run-finish cleanup, subagent progress
  pid/snippet persistence, engine-switch runtime metadata, and
  finish-run queue reconciliation. Existing tests already covered run start,
  stage finish, subagent start/finish, task outcome diagnostics, and removed
  legacy runtime fields. Verified with
  `uv run pytest tests/tasks/test_runtime_updates.py tests/config/test_engine_freeze.py -q`
  and `make typecheck`.

- [x] P4.2. Introduce `TaskRuntimeTransitions`.

  Constructor dependencies:
  - `workspace: Workspace`
  - `tasks: WorkspaceTasks`
  - mutation guard / lock collaborator
  - clock callable

  Methods:
  - `start_run(task)`
  - `finish_run(task, final_status)`
  - `start_stage(task, stage)`
  - `finish_stage(task, stage, status)`
  - `mark_subagent_started(task, subagent, pid=None)`
  - `mark_subagent_progress(task, subagent, execution)`
  - `mark_subagent_finished(task, subagent, result)`
  - `switch_engine(task, from_engine, to_engine, reason)`
  - `record_outcome(task, outcome)`
  - `clear_run_activity(task, execution_status)`

  Verification:
  - [x] Runtime-transition tests call the service directly, and old wrapper
    names have no matches.
  - [x] `make typecheck`

  Completed 2026-05-08: introduced `TaskRuntimeTransitions` in
  `litehive/tasks/runtime.py` with injected `Workspace`, `WorkspaceTasks`, and
  clock dependencies. The service owns persisted runtime transitions for run
  start/finish, finish-run queue reconciliation, retry/outcome state, stage
  start/finish, subagent start/pid/progress/finish, and engine switches. The
  old runtime-transition wrappers were recorded as TW16-TW28 in the Temporary
  Wrapper Cleanup Ledger and later deleted. Current tests call
  `task_runtime_transitions_for_workspace(workspace)` directly. Verified the
  service path with
  `rg -n "class TaskRuntimeTransitions|task_runtime_transitions_for_workspace|def mark_task_run_started_for_workspace|def finish_task_run_transition_for_workspace|def mark_engine_switch_for_workspace" litehive/tasks/runtime.py tests/tasks/test_runtime_updates.py`,
  then ran
  `uv run pytest tests/tasks/test_runtime_updates.py tests/config/test_engine_freeze.py -q`
  and `make typecheck`.

- [x] P4.3. Route lifecycle/engine adapter callers through the object.

  Verification:
  - [x] `rg -n "mark_.*_for_workspace|finish_task_run_transition_for_workspace" litehive`
  - [x] The exact command above returns no matches.
  - [x] `uv run pytest tests/lifecycle tests/tasks -q`

  Completed 2026-05-08: routed `SubagentManager`,
  `SubagentSessionManager`, and engine-switch code through
  `task_runtime_transitions_for_workspace(...)`. Later cleanup removed the old
  runtime-transition wrappers; current callers use the service directly.
  Verified with `make typecheck` and
  `uv run pytest tests/agents tests/lifecycle tests/tasks -q`.

  Continuation verification 2026-05-08: audited P4 again against the exact
  current code path. Moved the single-use `apply_task_run_started`,
  `apply_task_run_finished`, `apply_flag_count_auto_defer`,
  `apply_stage_started`, `apply_stage_finished`, `apply_subagent_started`,
  `apply_subagent_pid`, `apply_subagent_progress`, `apply_subagent_finished`,
  and `apply_engine_switch` free functions into private
  `TaskRuntimeTransitions` methods. Kept `clear_task_run_activity` and
  `apply_task_outcome` as shared in-memory helpers because status/recovery
  transition modules still batch those mutations with their own persistence.
  Verified no remaining matches for the moved function names with
  `rg -n "\\b(apply_task_run_started|apply_task_run_finished|apply_flag_count_auto_defer|apply_stage_started|apply_stage_finished|apply_subagent_started|apply_subagent_pid|apply_subagent_progress|apply_subagent_finished|apply_engine_switch)\\(" litehive tests tests_integration`.
  Ran
  `uv run pytest tests/tasks/test_runtime_updates.py tests/tasks/test_flag_auto_defer.py tests/config/test_engine_freeze.py -q`
  (`49 passed`) and `make typecheck`.

## Phase 5: TaskQueueService And PoolService

Reason: queue selection/mutation and pool reporting are related but should not
be one oversized class.

- [x] P5.1. Add characterization tests for queue selection and mutation.

  Verification:
  - [x] `uv run pytest tests/tasks tests/cli/test_pool.py -q` if present
  - [x] If there is no pool-specific file, run the relevant CLI/pool tests
    found by `rg -n "pool" tests`.

  Completed 2026-05-08: added
  `tests/tasks/test_queue_mutations.py` with characterization coverage for
  enqueue/front enqueue deduplication and audit context, one-based queue move
  with end clamping and audit context, multi-task prioritize ordering with
  per-task audit context, and active-task conflict rejection without queue
  mutation. Existing `tests/tasks/test_queue_invariants.py` and
  `tests/tasks/test_zombie_queue_regressions.py` cover selection, restore,
  blocked, terminal, and stale-active behavior. Verified pool-specific test
  presence with `rg -n "pool" tests`, then ran
  `uv run pytest tests/tasks/test_queue_mutations.py -q`,
  `uv run pytest tests/tasks tests/cli/test_pool.py -q`, and `make typecheck`.

- [x] P5.2. Introduce `TaskQueueService`.

  Methods:
  - `eligible_tasks()`
  - `select_next()`
  - `peek_next_selection()`
  - `dequeue_next()`
  - `peek_next()`
  - `enqueue(task_id)`
  - `move(task_id, position)`
  - `prioritize(task_ids)`
  - `remove(state, task_id)`
  - `mark_active(task_id)`
  - `clear_active()`
  - `restore_untouched_active()`
  - `active_task_markers(state=None)`
  - `validate_single_active_task(state=None)`
  - `is_resumable(task)`
  - `is_runnable(task)`
  - `remove_from_state(state, task_id)`
  - `restore_missing_from_state(state, tasks_by_id)`

  Verification:
  - [x] Public callers use `TaskQueueService`; lower-level implementation
    functions remain behind the service in `queue_mutations.py` and
    `queue_selection.py`.
  - [x] `make typecheck`

  Completed 2026-05-08: introduced `TaskQueueService` in
  `litehive/tasks/queue.py` as the workspace-bound public facade for queue
  selection, mutation, active-task markers, active-task validation, and
  runnable/resumable predicates. At this step, existing public free queue
  functions delegated through `task_queue_service_for_workspace(workspace)` or
  static state helper methods, and those temporary wrappers were tracked as
  TW29-TW43 in the Temporary Wrapper Cleanup Ledger. The public wrappers were
  later deleted from `litehive/tasks/queue.py`; lower-level implementation
  functions remain in `queue_mutations.py` and `queue_selection.py` behind
  `TaskQueueService`. Verified the exact wrapper path with
  `rg -n "def (enqueue_task_for_workspace|enqueue_task_front_for_workspace|move_queued_task_for_workspace|prioritize_queued_tasks_for_workspace|drop_task_from_workspace_state|set_active_task|clear_active_task|restore_untouched_active_task|active_task_markers_for_workspace|validate_single_active_task_for_workspace|peek_next_task|peek_next_task_selection|dequeue_next_task|dequeue_next_task_selection|restore_missing_queued_tasks)|class TaskQueueService|task_queue_service_for_workspace" litehive/tasks/queue.py`,
  then ran `uv run pytest tests/tasks tests/cli/test_pool.py -q` and
  `make typecheck`.

- [x] P5.3. Introduce `PoolService`.

  Methods:
  - `stage_outcomes(task_id)`
  - `task_report_entry(...)`
  - `collect_pending()`
  - `collect_resumable()`
  - `collect_closed()`
  - `summarize(completed, flagged, stop_reason, tasks_run=None)`
  - `render_summary(report)`
  - `render_summary_report(report)`
  - `write_summary(report)`
  - `print_summary(report)`
  - `print_summary_report(report, emit=...)`
  - `stop_for(reason)`
  - `run(limit=None)`

  Verification:
  - [x] CLI pool command only parses options and calls `PoolService`.
  - [x] `rg -n "task_stage_outcomes_for_workspace|_pending_pool_tasks_for_workspace|_resumable_pool_tasks_for_workspace" litehive`
    shows no legacy helper references.
  - [x] `make test`

  Completed 2026-05-08: introduced `PoolService` in `litehive/cli/pool.py`
  with workspace-bound methods for drain-loop policy, pending/resumable/closed
  collection, summary construction, summary rendering/writing, stop-reason
  persistence, and stage-outcome loading. `_run_drain` in
  `litehive/cli/runner.py` now constructs `PoolService` with injected
  `run_once` and dirty-check callbacks and delegates the pool loop through
  `PoolService.run`, leaving `run_command` responsible for option parsing and
  config fallback. At this step, existing pool free functions delegated to
  `PoolService` and were tracked as TW44-TW51; those wrappers were later
  deleted. Verified the exact legacy helper grep and focused verification with
  `uv run pytest tests/cli/test_pool.py tests/cli/test_entrypoint.py tests/tasks/test_zombie_queue_regressions.py::test_run_drain_skips_zombie_queue_entries_and_leaves_main_clean -q`
  and `make typecheck`; full verification passed with `make test`
  (`929 passed, 1 skipped`).

  Continuation verification 2026-05-08: audited P5 against the exact current
  code path. `litehive/tasks/queue.py` exposes `TaskQueueService` as the
  public queue owner while the lower-level implementation bodies remain in
  `queue_mutations.py` and `queue_selection.py` behind the service, matching
  P5.2. Production queue callers use `task_queue_service_for_workspace(...)`
  for selection, mutation, active-task markers, and validation. Verified the
  legacy pool helper grep has no matches with
  `rg -n "task_stage_outcomes_for_workspace|_pending_pool_tasks_for_workspace|_resumable_pool_tasks_for_workspace|_closed_pool_tasks_for_workspace|render_pool_summary|write_pool_summary|run_pool" litehive tests tests_integration`.
  Ran
  `uv run pytest tests/tasks/test_queue_mutations.py tests/tasks/test_queue_invariants.py tests/tasks/test_zombie_queue_regressions.py tests/cli/test_pool.py -q`
  (`24 passed`).

## Phase 6: TaskReportStore, TaskActivityStore, And TaskEventLog

Reason: reports, activity, and events are related persistence surfaces but
should remain separate stores.

- [x] P6.1. Introduce `TaskReportStore`.

  Verification:
  - [x] Stage/recovery report tests pass through the store.
  - [x] `rg -n "class TaskReportStore|def (insert_recovery_report|load_recovery_reports|latest_recovery_report|record_stage_report|rewrite_latest_stage_report|load_stage_reports_for_task_id|load_workspace_stage_reports|load_stage_reports|latest_stage_report)\\(" litehive/tasks/report_storage.py`
    shows the current store methods.

  Completed 2026-05-08: introduced `TaskReportStore` in
  `litehive/tasks/report_storage.py` with workspace-bound methods for
  recovery report insert/load/latest, stage report record/rewrite/load/latest,
  task-id stage report lookup, and workspace-wide stage report lookup. At this
  step, existing report-storage free functions delegated to
  `task_report_store_for_workspace(workspace)` and were tracked as TW52-TW60
  in the Temporary Wrapper Cleanup Ledger; those wrappers were later deleted.
  Routed production stage-report writers
  in `SubagentManager`, hook report persistence, and `PoolService.stage_outcomes`
  through the store directly. Routed the recovery report application service to
  `TaskReportStore.insert_recovery_report` and
  `TaskReportStore.latest_stage_report`; `record_recovery_report(...)` is now
  the application service in `litehive/tasks/recovery_reports.py`, not a raw
  storage wrapper. Verified the exact grep command, then ran
  `uv run pytest tests/tasks/test_event_log_rebuild.py tests/state/test_task_runtime_storage.py tests/lifecycle/test_engine_adapter.py tests/lifecycle/test_hooks_and_commit.py tests/agents/test_subagent_manager.py tests/recovery/test_runner_recovery.py tests/recovery/test_repair.py -q`
  and `make typecheck`.

- [x] P6.2. Introduce `TaskActivityStore`.

  Verification:
  - [x] `TaskActivityStore` exposes `load`, `save`, `append`,
    `latest_entry`, and `latest`.
  - [x] `rg -n "Workspace\\.task_activity|\\.task_activity\\(|TaskActivityLog|load_task_activity\\(|save_task_activity\\(|append_task_activity\\(|latest_task_activity_entry\\(" litehive tests tests_integration`
    has no matches.

  Completed 2026-05-08: introduced `TaskActivityStore` in
  `litehive/tasks/activity.py` and added `task_activity_store_for_task(...)`.
  The old `Workspace.task_activity(...)`, `TaskActivityLog`, and activity
  helper wrappers were later deleted during TW61-TW66 and P13.2. Current
  callers use `task_activity_store_for_task(workspace, task)` directly.
  Verified with the exact `rg` commands above, then ran
  `uv run pytest tests/tasks/test_activity.py tests/cli/test_agent_report.py tests/lifecycle/test_prompt_serializer.py -q`
  and `make typecheck`.

- [x] P6.3. Introduce `TaskEventLog`.

  Verification:
  - [x] event append/read/rebuild tests pass.
  - [x] `rg -n "class TaskEventLog|def (path|append|read|has_events|rebuild_sqlite|sqlite_task_tables_empty)\\(|task_event_log_for_workspace" litehive/tasks/event_log.py tests/tasks/test_event_log_rebuild.py tests/config/test_workspace_bootstrap.py tests/state/test_db_migrations.py`
    shows the current event-log methods and direct tests.
  - [x] `rg -n "^def (task_event_log_path|append_task_event|read_task_events|task_event_log_has_events|rebuild_sqlite_from_task_event_log|sqlite_task_tables_empty)\\(" litehive/tasks/event_log.py`
    has no module-level wrapper matches.

  Completed 2026-05-08: introduced `TaskEventLog` in
  `litehive/tasks/event_log.py` with workspace-bound methods for path lookup,
  append, read, valid-event probing, SQLite replay, and SQLite task-table empty
  checks. At this step, existing event-log free functions delegated to
  `task_event_log_for_workspace(workspace)` and were tracked as TW67-TW72 in
  the Temporary Wrapper Cleanup Ledger; those wrappers were later deleted.
  Current callers use `task_event_log_for_workspace(workspace)` directly.
  Verified the exact grep commands and behavior with
  `uv run pytest tests/tasks/test_event_log_rebuild.py tests/config/test_workspace_bootstrap.py tests/state/test_db_migrations.py::test_legacy_workspace_db_rebuild_replays_task_event_log_without_task_yaml_rescan -q`
  and `make typecheck`.

- [x] P6.4. Remove `Workspace.append_event(...)` after callers use
  `TaskEventLog`.

  Verification:
  - [x] `rg -n "\\.append_event\\(" litehive tests`
  - [x] `make test`

  Completed 2026-05-08: routed the subagent session/manager event writers and
  session-event tests directly through `litehive.observability.events.append_event`,
  then deleted `Workspace.append_event(...)`. Verified the exact grep command
  returns no matches, then ran
  `uv run pytest tests/agents/test_session_events.py tests/agents/test_subagent_manager.py -q`,
  `make typecheck`, and `make test` (`929 passed, 1 skipped`).

  Continuation verification 2026-05-08: audited P6 against the exact current
  code path. `TaskReportStore`, `TaskActivityStore`, and `TaskEventLog` are
  the current storage owners; production callers use
  `task_report_store_for_workspace(...)`, `task_activity_store_for_task(...)`,
  and `task_event_log_for_workspace(...)`. Verified no stale
  `Workspace.task_activity(...)`, `TaskActivityLog`, old activity helper, or
  module-level event-log wrapper calls with
  `rg -n "Workspace\\.task_activity|\\.task_activity\\(|TaskActivityLog|load_task_activity\\(|save_task_activity\\(|append_task_activity\\(|latest_task_activity_entry\\(|task_event_log_path\\(|append_task_event\\(|read_task_events\\(|task_event_log_has_events\\(|rebuild_sqlite_from_task_event_log\\(|sqlite_task_tables_empty\\(" litehive tests tests_integration`.
  Ran
  `uv run pytest tests/tasks/test_activity.py tests/tasks/test_event_log_rebuild.py tests/config/test_workspace_bootstrap.py tests/state/test_db_migrations.py::test_legacy_workspace_db_rebuild_replays_task_event_log_without_task_yaml_rescan tests/cli/test_agent_report.py tests/lifecycle/test_prompt_serializer.py -q`
  (`100 passed`).

## Phase 7: StatusSnapshotCollector

Reason: status currently has tolerant config/state loading and many probes.
Those belong behind one read-only collector with injected probe collaborators.

- [x] P7.1. Add characterization tests for status with corrupt/missing inputs.

  Verification:
  - [x] `uv run pytest tests/observability/test_status_diagnostics.py tests/observability -q`

  Completed 2026-05-08: added
  `test_status_uses_defaults_when_workspace_config_is_missing` to characterize
  the missing workspace-config path before extracting the status collector.
  Existing tests already cover missing database, corrupt config YAML, invalid
  merged config, non-mapping config, corrupt workspace DB, corrupt runner lock,
  missing active task records, and queued tasks missing from SQLite. Verified
  with `uv run pytest tests/observability/test_status_diagnostics.py tests/observability -q`
  and `make typecheck`.

- [x] P7.2. Introduce `StatusSnapshotCollector`.

  Methods:
  - `collect()`
  - `collect_operational()`
  - `load_config()`
  - `load_state()`
  - `probe_runner()`
  - `probe_daemon()`
  - `probe_origin_divergence()`
  - `probe_recovery_failure()`

  Verification:
  - [x] Status callers use `status_snapshot_collector_for_workspace(...)`
    directly.
  - [x] `make typecheck`

  Completed 2026-05-08: introduced `StatusSnapshotCollector` in
  `litehive/observability/status_diagnostics.py` with workspace-bound methods
  for full and operational snapshot collection, config/state loading, runner
  probing, daemon probing, origin-divergence probing, and recovery-failure
  probing. The old status collection wrappers were tracked as TW73-TW74 and
  later deleted; current callers use
  `status_snapshot_collector_for_workspace(workspace)` directly. Verified the
  exact collector path with
  `rg -n "class StatusSnapshotCollector|def (collect|collect_operational|load_config|load_state|probe_runner|probe_daemon|probe_origin_divergence|probe_recovery_failure)\\(|status_snapshot_collector_for_workspace" litehive/observability/status_diagnostics.py tests/observability/test_status_diagnostics.py tests/observability/test_task_summary.py litehive/observability/status.py`,
  then ran `uv run pytest tests/observability/test_status_diagnostics.py tests/observability -q`
  and `make typecheck`.

- [x] P7.3. Route CLI/status callers to the collector through the container.

  Verification:
  - [x] `rg -n "collect_status_snapshot_for_workspace|collect_operational_status_snapshot_for_workspace" litehive`
    has no matches.
  - [x] `make test`

  Completed 2026-05-08: routed `collect_task_pipeline_status_for_workspace`
  through `status_snapshot_collector_for_workspace(workspace)` and updated the
  status summary characterization test to patch the collector factory instead
  of the old module-level collection wrapper. The exact legacy wrapper grep now
  has no matches.
  Verified with
  `uv run pytest tests/observability/test_status_diagnostics.py tests/observability/test_task_summary.py -q`,
  `make typecheck`, and `make test` (`930 passed, 1 skipped`).

  Continuation verification 2026-05-08: audited P7 against the exact current
  code path. The deleted `collect_status_snapshot_for_workspace` and
  `collect_operational_status_snapshot_for_workspace` wrappers have no
  production/test matches; `collect_task_pipeline_status_for_workspace(...)`
  remains the public status orchestration function and constructs
  `status_snapshot_collector_for_workspace(workspace)` directly before calling
  `collector.collect()` or `collector.collect_operational()`. Verified with
  `rg -n "status_snapshot_collector_for_workspace|collect_status_snapshot_for_workspace|collect_operational_status_snapshot_for_workspace" litehive tests tests_integration`.
  Ran
  `uv run pytest tests/observability/test_status_diagnostics.py tests/observability/test_task_summary.py -q`
  (`42 passed`).

## Phase 8: EngineRoutingPolicy

Reason: default/preference/freeze/quota/recovery routing is policy, not
engine adapter lookup.

- [x] P8.1. Add characterization tests for engine routing.

  Verification:
  - [x] `uv run pytest tests/config/test_engine_freeze.py tests/config/test_engine_models.py -q`

  Completed 2026-05-08: added
  `test_select_engine_uses_explicit_request_candidates_after_exclusions` to
  characterize `EngineSelectionRequest.engine_names`,
  `excluded_engine_names`, requested model propagation, and quota-check bypass.
  Existing tests cover default/preference routing, active and expired freezes,
  quota freeze fallback, availability checks, recovery engine selection, engine
  switch precedence, and model override precedence. Verified with
  `uv run pytest tests/config/test_engine_freeze.py tests/config/test_engine_models.py -q`
  and `make typecheck`.

- [x] P8.2. Introduce `EngineRoutingPolicy`.

  Methods:
  - `select(task, request=None)`
  - `resolve_engine_name(name)`
  - `resolve_model_override(task, engine)`
  - `resolve_recovery_engine(task)`
  - `freeze(engine, until, reason)`
  - `unfreeze(engine, reason)`
  - `set_default(engine, reason)`
  - `set_preference(order, reason)`
  - `quota_status(engine)`
  - `clear_expired_freezes()`

  Verification:
  - [x] `EngineRoutingPolicy` exposes the current routing/control methods.
  - [x] `select_engine_for_workspace(...)` has no code calls.
  - [x] `make typecheck`

  Completed 2026-05-08: introduced `EngineRoutingPolicy` in
  `litehive/config/engine_models.py` with workspace-bound methods for engine
  selection, engine-name resolution, model override resolution, recovery-engine
  resolution, freeze/unfreeze/default/preference persistence, quota status, and
  expired-freeze cleanup. The old `select_engine_for_workspace(...)` wrapper
  was tracked as TW75 and later deleted. Pure helpers such as
  `resolve_engine_name(...)` and `resolve_model(...)` remain module-level
  compatibility/utility APIs, while CLI and lifecycle callers use
  `engine_routing_policy_for_workspace(...)`. Verified with
  `rg -n "class EngineRoutingPolicy|def (select|resolve_engine_name|resolve_model_override|resolve_recovery_engine|freeze|unfreeze|set_default|set_preference|quota_status|clear_expired_freezes)\\(|engine_routing_policy_for_workspace" litehive/config/engine_models.py`,
  `rg -n "select_engine_for_workspace\\(" litehive tests tests_integration`,
  `uv run pytest tests/config/test_engine_freeze.py tests/config/test_engine_models.py -q`,
  and `make typecheck`.

- [x] P8.3. Route CLI and lifecycle engine selection through
  `EngineRoutingPolicy`.

  Verification:
  - [x] `rg -n "select_engine|resolve_engine_name|resolve_recovery_engine|set_engine" litehive`
  - [x] `make test`

  Completed 2026-05-08: routed lifecycle selection through
  `engine_routing_policy_for_workspace(...).select(...)`, preview engine
  selection through the policy, recovery-engine selection through
  `EngineRoutingPolicy.resolve_recovery_engine(...)`, and CLI
  default/preference/freeze/unfreeze changes through policy methods. Updated
  engine-routing tests to patch the policy factory seam rather than the old
  free-function selector import. The verification grep now shows policy
  internals, intentionally retained pure helper APIs, runtime-setting helpers,
  the recovery-engine facade, and merge-resolution's intentional use of that
  facade; it no longer shows the deleted `select_engine_for_workspace(...)`
  wrapper. Verified with
  `uv run pytest tests/config/test_engine_freeze.py tests/config/test_engine_models.py tests/lifecycle/test_engine_adapter.py tests/lifecycle/test_hooks_and_commit.py tests/cli/test_entrypoint.py -q`,
  `make typecheck`, and `make test` (`931 passed, 1 skipped`).

  Continuation verification 2026-05-08: audited P8 against the exact current
  code path. The deleted `select_engine_for_workspace(...)` and engine-control
  workspace wrappers have no production/test matches; lifecycle selection,
  preview selection, recovery-engine selection, and CLI engine control route
  through `engine_routing_policy_for_workspace(...)`. Pure helpers such as
  `resolve_engine_name(...)` and `resolve_model(...)` remain module-level
  utility APIs. Verified with
  `rg -n "class EngineRoutingPolicy|engine_routing_policy_for_workspace|select_engine_for_workspace|resolve_engine_name_for_workspace|resolve_model_override_for_workspace|resolve_recovery_engine_for_workspace|freeze_engine_for_workspace|unfreeze_engine_for_workspace|set_default_engine_for_workspace|set_engine_preference_for_workspace|engine_quota_status_for_workspace|clear_expired_engine_freezes_for_workspace" litehive tests tests_integration`.
  Ran
  `uv run pytest tests/config/test_engine_freeze.py tests/config/test_engine_models.py tests/lifecycle/test_engine_adapter.py tests/lifecycle/test_hooks_and_commit.py tests/cli/test_entrypoint.py -q`
  (`124 passed, 1 skipped`).

## Phase 9: Worktree Service Split

Reason: the old `WorktreeService` facade mixed sync, cleanup, rescue, and
inspection. The current target is four focused workspace-bound services plus
small path/git helpers that stay as utilities.

- [x] P9.1. Add characterization tests for each worktree responsibility.

  Cover:
  - [x] sync task worktree
  - [x] inspect task worktree
  - [x] cleanup terminal task worktree
  - [x] collect/apply rescue candidates
  - [x] prune stale worktrees

  Verification:
  - [x] `uv run pytest tests/cli/test_worktree_support.py tests/cli/test_worktree_rescue.py tests/lifecycle/test_worktree_sync.py -q`

  Completed 2026-05-08: existing lifecycle tests cover worktree creation,
  reuse, rebase, dirty-skip behavior, and stale git metadata pruning; existing
  rescue CLI tests cover applying clean, already-landed, missing-worktree,
  active-task, and manual-conflict candidates. Added direct
  worktree characterization tests for task worktree inspection,
  terminal cleanup metadata/disk removal, and rescue candidate collection so
  the upcoming service split has coverage for each focused responsibility.
  Verified with
  `uv run pytest tests/cli/test_worktree_support.py tests/cli/test_worktree_rescue.py tests/lifecycle/test_worktree_sync.py -q`
  and `make typecheck`.

- [x] P9.2. Introduce `WorktreeInspector`.

  Verification:
  - [x] read-only inspection callers route through inspector.

  Completed 2026-05-08: introduced `WorktreeInspector` in
  `litehive/worktree/inspection.py` with
  `inspect_task_worktree(task)`. Routed task debug worktree evidence and the
  service's internal missing-worktree probe through the inspector. Tracked the
  temporary facade as TW76, then deleted it in P9.6. Verified the exact route with
  `rg -n "WorktreeService\\(.+inspect_task_worktree|inspect_task_worktree\\(" litehive tests`,
  `uv run pytest tests/cli/test_worktree_support.py tests/cli/test_task_debug.py tests/cli/test_agent_report.py -q`,
  and `make typecheck`.

- [x] P9.3. Introduce `WorktreeSyncService`.

  Verification:
  - [x] lifecycle sync callers route through sync service.

  Completed 2026-05-08: introduced `WorktreeSyncService` in
  `litehive/worktree/sync.py` for task worktree create/reuse/rebase/upstream
  merge behavior. Routed `GitWorktreeSyncNode.sync(...)` through
  `WorktreeSyncService.sync_task_worktree(...)`; tracked the temporary facade
  as TW77, then deleted it in P9.6. Verified the exact route with
  `rg -n "sync_task_worktree\\(|WorktreeSyncService|WorktreeService\\(self\\.workspace\\).*sync" litehive tests`,
  `uv run pytest tests/lifecycle/test_worktree_sync.py tests/cli/test_worktree_support.py -q`,
  and `make typecheck`.

- [x] P9.4. Introduce `WorktreeCleanupService`.

  Verification:
  - [x] cleanup callers route through cleanup service.

  Completed 2026-05-08: introduced `WorktreeCleanupService` in
  `litehive/worktree/cleanup.py` for managed-worktree listing, cleanable
  worktree removal, and terminal task worktree cleanup. Routed `worktree ls`,
  `worktree clean`, workspace status worktree listing, and terminal lifecycle
  cleanup through the cleanup service. Tracked the matching temporary facade
  methods as TW78-TW80, then deleted them in P9.6. Verified with
  `rg -n "collect_managed_worktrees|remove_cleanable_worktrees|cleanup_terminal_task_worktree|WorktreeCleanupService|WorktreeService\\(" litehive tests`,
  `uv run pytest tests/cli/test_worktree_support.py tests/cli/test_worktree_clean_with_active_runner.py tests/tasks/test_worktrees.py tests/observability/test_task_summary.py -q`,
  and `make typecheck`.

- [x] P9.5. Introduce `WorktreeRescueService`.

  Verification:
  - [x] rescue callers route through rescue service.

  Completed 2026-05-08: introduced `WorktreeRescueService` in
  `litehive/worktree/rescue.py` for rescue candidate collection, clean-main
  guarding, and candidate application. Routed `worktree rescue` through the
  rescue service. Tracked the matching temporary facade methods as TW81-TW83,
  then deleted them in P9.6. Verified with
  `rg -n "collect_rescue_candidates|apply_rescue_candidate|require_clean_main_checkout|WorktreeRescueService|WorktreeService\\(" litehive tests/cli/test_worktree_rescue.py tests/cli/test_worktree_support.py`,
  `uv run pytest tests/cli/test_worktree_rescue.py tests/cli/test_worktree_support.py -q`,
  and `make typecheck`.

- [x] P9.6. Keep `WorktreeService` only as a temporary facade or delete it if
  all callers use focused services.

  Verification:
  - [x] `rg -n "WorktreeService" litehive tests`
  - [x] `make test`

  Completed 2026-05-08: deleted `litehive/worktree/service.py` after routing
  the remaining stale-worktree probe and repair closures directly through
  `WorktreeInspector` and `WorkspaceTasks`. Updated stale comments to name the
  focused services. Verified `rg -n "WorktreeService" litehive tests` returns
  no matches, `make typecheck`, and `make test` (`934 passed, 1 skipped`).

- [x] RD23. Phase 9's reason text still described `WorktreeService` as a
  current useful facade even though P9.6 deleted it.

  Delete when:
  - [x] `rg -n "WorktreeService is [a] useful facade|currently mixe[s] sync" docs/object-refactor-plan-2026-05-08.md`
    has no matches.
  - [x] `rg -n "WorktreeService" litehive tests`
    has no production or test code matches.
  - [x] `rg -n "class WorktreeInspector|class WorktreeSyncService|class WorktreeCleanupService|class WorktreeRescueService" litehive/worktree`
    shows the four focused worktree owners.

  Completed 2026-05-08: refreshed the Phase 9 reason to name the deleted
  facade and the current focused service shape. Verified the exact class owners
  and that code no longer imports or instantiates `WorktreeService`.

## Phase 10: RuntimeStore Internal Split

Reason: `RuntimeStore` is a persistence facade with too many table families.

- [x] P10.1. Add characterization tests for each store family.

  Families:
  - [x] workspace state
  - [x] task state
  - [x] task intent
  - [x] process state
  - [x] subagent counters
  - [x] bootstrap/rebuild

  Verification:
  - [x] `uv run pytest tests/state -q`

  Completed 2026-05-08: added
  `tests/state/test_runtime_store_characterization.py` with direct coverage
  for workspace state round-trip and queue/pool split, task intent/state
  round-trip and indexed status mirroring, process state save/load/clear,
  highest task-number scanning, SQLite-backed subagent id counters seeded from
  persisted sessions, and bootstrap-triggered event-log rebuild. Verified with
  `uv run pytest tests/state/test_runtime_store_characterization.py tests/state -q`
  (`58 passed`) and `make typecheck`.

- [x] P10.2. Introduce `WorkspaceStateStore`.

  Completed 2026-05-08: introduced `WorkspaceStateStore` in
  `litehive/state/store.py` for workspace pool/queue state load, read-only
  load, save, transactional save, replay-event emission, and singleton row
  creation. `RuntimeStore.load_workspace_state`,
  `load_workspace_state_read_only`, `save_workspace_state`,
  `_save_workspace_state`, `_append_workspace_state_event`, and bootstrap row
  creation now delegate to `WorkspaceStateStore`; the unused static
  `RuntimeStore.create_workspace_state_rows(...)` shim was deleted in RD24.
  Verified the exact method path with
  `rg -n "class WorkspaceStateStore|def (load_workspace_state|load_workspace_state_read_only|save_workspace_state|create_workspace_state_rows|_save_workspace_state|_append_workspace_state_event)" litehive/state/store.py`,
  `uv run pytest tests/state/test_runtime_store_characterization.py tests/state -q`
  (`58 passed`), and `make typecheck`.
- [x] P10.3. Introduce `TaskStateStore`.

  Completed 2026-05-08: introduced `TaskStateStore` in
  `litehive/state/store.py` for task state load/save, task runtime load/save,
  and transactional task-state upserts with intent status mirroring.
  `RuntimeStore.load_task_state`, `save_task_state`, `load_task_runtime`,
  `save_task_runtime`, and `_save_task_state` now delegate to the task-state
  store. Verified the exact method path with
  `rg -n "class TaskStateStore|def (load_task_state|save_task_state|load_task_runtime|save_task_runtime|_save_task_state)" litehive/state/store.py`,
  `uv run pytest tests/state/test_runtime_store_characterization.py tests/state/test_task_runtime_storage.py tests/state -q`
  (`58 passed`), and `make typecheck`.
- [x] P10.4. Introduce `TaskIntentStore`.

  Completed 2026-05-08: introduced `TaskIntentStore` in
  `litehive/state/store.py` for task intent load/list/save and transactional
  intent upserts with denormalized query columns. `RuntimeStore.load_task_intent`,
  `list_task_intents`, `save_task_intent`, and `_save_task_intent` now delegate
  to the task-intent store. Verified the exact method path with
  `rg -n "class TaskIntentStore|def (load_task_intent|list_task_intents|save_task_intent|_save_task_intent)" litehive/state/store.py`,
  `uv run pytest tests/state/test_runtime_store_characterization.py tests/state/test_task_runtime_storage.py tests/state -q`
  (`58 passed`), and `make typecheck`.
- [x] P10.5. Introduce `ProcessStateStore`.

  Completed 2026-05-08: introduced `ProcessStateStore` in
  `litehive/state/store.py` for runtime process save/load/clear behavior.
  `RuntimeStore.save_process_state`, `load_process_state`, and
  `clear_process_state` now delegate to the process-state store. Verified the
  exact method path with
  `rg -n "class ProcessStateStore|def (save_process_state|clear_process_state|load_process_state)" litehive/state/store.py`,
  `uv run pytest tests/state/test_runtime_store_characterization.py tests/state/test_process_state.py tests/state -q`
  (`58 passed`), and `make typecheck`.
- [x] P10.6. Introduce `SubagentCounterStore`.

  Completed 2026-05-08: introduced `SubagentCounterStore` in
  `litehive/agents/subagent_ids.py` for SQLite-backed numeric subagent id
  reservation. `SubagentIdRepository.reserve_next_id(...)` now delegates to
  `SubagentCounterStore.reserve_next_number(...)` and only formats the returned
  number as a `SubagentId`. Verified the exact method path with
  `rg -n "class SubagentCounterStore|class SubagentIdRepository|reserve_next_number|reserve_next_id" litehive/agents/subagent_ids.py tests/agents/test_subagent_ids.py tests/state/test_runtime_store_characterization.py`,
  `uv run pytest tests/agents/test_subagent_ids.py tests/state/test_runtime_store_characterization.py tests/state -q`
  (`60 passed`), and `make typecheck`.

  Verification for each split:
  - [x] Old `RuntimeStore` method delegates to the new internal store.
  - [x] Focused state tests pass.
  - [x] `make typecheck`

- [x] P10.7. Decide whether to keep `RuntimeStore` as a facade.

  Decision rule:
  - [x] Keep facade if it simplifies callers and does not regain policy.
  - [x] Do not delete facade until the container cleanly exposes focused stores.

  Completed 2026-05-08: keep `RuntimeStore` as the public persistence facade
  for now. Caller scan shows it still simplifies workspace bootstrap, status,
  queue selection, records, locking, daemon registry, and journal call sites;
  the new focused stores own table-family details while `RuntimeStore` keeps
  transaction composition and existing factory/test seams. Verified with
  `rg -n "RuntimeStore|runtime_store_for_workspace" litehive tests | head -n 80`,
  `make typecheck`, and `make test` (`940 passed, 1 skipped`).

- [x] RD24. Phase 10.2 still described a deleted-shim candidate as a retained
  static compatibility method.

  Delete when:
  - [x] `rg -n "RuntimeStore\\.create_workspace_state_rows|def create_workspace_state_rows" litehive/state/store.py`
    shows only `WorkspaceStateStore.create_workspace_state_rows(...)`.
  - [x] `rg -n "static [`]create_workspace_state_row[s][`] compatibility method|Delete facade only i[f]" docs/object-refactor-plan-2026-05-08.md`
    has no matches.
  - [x] `uv run pytest tests/state/test_runtime_store_characterization.py tests/state/test_db_migrations.py tests/state/test_task_runtime_storage.py -q`

  Completed 2026-05-08: removed the unused
  `RuntimeStore.create_workspace_state_rows(...)` delegating shim and updated
  P10.2/P10.7 to describe the remaining facade decision without leaving an
  unchecked alternative branch in the plan.

## Phase 11: Subagent Session And Report Boundaries

Reason: `SubagentManager` should coordinate, not own parsing, rendering, or
persistence details.

- [x] P11.1. Move remaining subagent artifact load/save helpers to
  `SubagentArtifactStore`.

  Verification:
  - [x] `rg -n "load_subagent_session|load_subagent_report|load_subagent_event_stream|subagent_artifacts" litehive`

  Completed 2026-05-08: routed recovery prompts, recovery evidence,
  interrupted-subagent repair, execution trace rendering, and workspace
  subagent-session convenience methods through `subagent_artifacts(...).load_*`
  / `.save(...)`. The `load_subagent_*` compatibility wrappers were deleted
  during TW87-TW91; current production `subagent_artifacts` calls are direct
  store bindings. Verified with
  `rg -n "load_subagent_session|load_subagent_report|load_subagent_event_stream|subagent_artifacts" litehive`,
  `uv run pytest tests/agents tests/recovery tests/cli/test_task_debug.py tests/cli/test_agent_report.py tests/lifecycle/test_prompt_serializer.py -q`
  (`206 passed`), and `make typecheck`.

- [x] P11.2. Introduce or complete `AgentReportService`.

  Verification:
  - [x] No module-level `stage_report_from_subagent(...)` helper remains.
  - [x] `SubagentManager` calls `AgentReportService.stage_report_from_subagent(...)`.
  - [x] `uv run pytest tests/agents tests/lifecycle/test_engine_adapter.py -q`

  Completed 2026-05-08: introduced `AgentReportService` in
  `litehive/agents/report_extraction.py` for building stage reports from
  subagent activity submissions. `SubagentManager._parse_execution_report(...)`
  now uses the service directly, and the old `stage_report_from_subagent(...)`
  function was deleted during TW92. Verified with
  `rg -n "^def stage_report_from_subagent|class AgentReportService|def stage_report_from_subagent|stage_report_from_subagent\\(" litehive/agents litehive tests/agents tests/lifecycle/test_engine_adapter.py`,
  `uv run pytest tests/agents tests/lifecycle/test_engine_adapter.py -q`
  (`154 passed`), and `make typecheck`.

- [x] P11.3. Ensure `SubagentManager` only coordinates run sequence.

  Verification:
  - [x] Manager no longer parses report payloads directly.
  - [x] Manager no longer renders execution traces directly.
  - [x] Manager persists sessions through injected session/artifact services.

  Completed 2026-05-08: moved execution-trace rendering behind
  `SubagentSessionManager.render_execution_trace(...)`, moved direct
  terminal/live artifact writes behind `SubagentSessionManager`
  artifact methods, and kept stage-report extraction behind
  `AgentReportService`. `SubagentManager` still shapes snapshot
  `SubagentReportPayload` objects for session persistence, but report parsing
  and artifact/session writes now live in collaborators. Verified with
  `rg -n "execution_trace_renderer|write_stream_artifact|write_text_if_changed|subagent_artifacts|write_session_snapshot|SubagentReportPayload|AgentReportService" litehive/agents/manager.py litehive/agents/session.py`,
  `uv run pytest tests/agents tests/lifecycle/test_engine_adapter.py -q`
  (`154 passed`), `make typecheck`, and `make test` (`940 passed, 1 skipped`).

## Phase 12: Daemon Object Boundary

Reason: daemon execution should be an object assembled by the daemon container.

- [x] P12.1. Add characterization tests for daemon loop behavior.

  Verification:
  - [x] `uv run pytest tests/daemon -q`

  Completed 2026-05-08: existing `tests/daemon/test_execution.py` already
  characterizes daemon loop behavior for waiting on a live runner, processing
  queued tasks sequentially, shared status snapshot loading, transient vs.
  terminal stop-reason policy, and typed runner liveness. Verified with
  `uv run pytest tests/daemon -q` (`14 passed`).

- [x] P12.2. Introduce `WorkspaceDaemon`.

  Methods:
  - `run()`
  - `run_cycle()`
  - `should_continue(stop_reason)`
  - `sleep_with_stop(seconds)`
  - `maybe_backup()`
  - `stop()`

  Verification:
  - [x] `build_daemon_container(...)` assembles daemon collaborators.
  - [x] CLI daemon command delegates to `WorkspaceDaemon`.

  Completed 2026-05-08: introduced `WorkspaceDaemon` in
  `litehive/daemon/execution.py` with explicit `run`, `run_cycle`,
  `should_continue`, `sleep_with_stop`, `maybe_backup`, and `stop`
  methods. `run_daemon_loop(...)` remains the process-boundary wrapper:
  it creates the workspace, obtains daemon collaborators from
  `build_daemon_container(...)`, and constructs `WorkspaceDaemon`.
  The CLI foreground and background worker commands continue to delegate
  through `litehive.cli.runner.daemon_worker(...)` into
  `run_daemon_loop(...)`, which now delegates to `WorkspaceDaemon`.
  The temporary `DaemonExecutor` compatibility alias was later deleted during
  TW84.

  Verification commands:
  - `uv run pytest tests/daemon -q` (`14 passed`)
  - `make typecheck` (`0 errors`)

- [x] P12.3. Introduce/refine `DaemonExecution`.

  Methods:
  - `pick_next_task()`
  - `run_task(task)`
  - `handle_result(result)`
  - `record_cycle_start()`
  - `record_cycle_finish()`

  Verification:
  - [x] daemon free functions are wrappers or removed.
  - [x] `make test`

  Completed 2026-05-08: introduced `DaemonExecution` and
  `DaemonRunIteration` in `litehive/daemon/task_execution.py`.
  The object owns one spawned `litehive run` cycle: `record_cycle_start`,
  `pick_next_task`, `run_task`, `handle_result`, and
  `record_cycle_finish`. `litehive.cli.runner.run_once(...)` was deleted
  during TW85; the single-shot CLI path and drain callback now construct
  `DaemonExecution(...).run_once()` directly with the CLI monkey-patch seams
  (`run_task` and `pick_next_task`) injected as collaborators.
  The old `_existing_consecutive_task_failure_stop` and
  `_emit_consecutive_task_failure_stop` free functions were removed;
  their behavior now lives on `DaemonExecution`.

  Verification commands:
  - `rg -n "class DaemonExecution|def pick_next_task|def run_task|def handle_result|def record_cycle_start|def record_cycle_finish|def run_once\\(" litehive`
  - `uv run pytest tests/cli/test_entrypoint.py tests/lifecycle/test_hook_reject_circuit_breaker.py tests/daemon -q` (`32 passed`)
  - `make typecheck` (`0 errors`)
  - `make test` (`940 passed, 1 skipped`)

- [x] RD25. Phase 12.3 still described `litehive.cli.runner.run_once(...)` as
  a retained compatibility wrapper even though TW85 deleted it.

  Delete when:
  - [x] `rg -n "def run_once\\(" litehive/cli litehive/daemon`
    shows only `DaemonExecution.run_once(...)`.
  - [x] `rg -n "thin compatibilit[y] wrapper|Temporary wrapper cleanup is tracked as TW8[5]" docs/object-refactor-plan-2026-05-08.md`
    has no matches.
  - [x] `uv run pytest tests/cli/test_entrypoint.py tests/lifecycle/test_hook_reject_circuit_breaker.py tests/daemon -q`

  Completed 2026-05-08: refreshed P12.3 to name the current direct
  `DaemonExecution(...).run_once()` callers in the single-shot CLI path and
  drain callback, and to remove the obsolete wrapper-cleanup note.

## Phase 13: Trim Workspace

Reason: after focused services exist, `Workspace` should stop acting as a
service locator.

- [x] P13.1. Remove task convenience methods once callers use
  `WorkspaceTasks`.

  Verification:
  - [x] `rg -n "\\.list_tasks\\(|\\.get_task\\(|\\.require_task\\(|\\.save_task\\(" litehive tests`

  Completed 2026-05-08: verified `litehive/workspace.py` no longer defines
  `list_tasks`, `get_task`, `require_task`, or `save_task`; production code
  uses `WorkspaceTasks` / `workspace_tasks_for_workspace(...)` instead. The
  exact `rg` command above returns no matches.

- [x] P13.2. Remove event/activity convenience methods once callers use
  `TaskEventLog` and `TaskActivityStore`.

  Verification:
  - [x] `rg -n "\\.append_event\\(|\\.task_activity\\(" litehive tests`

  Completed 2026-05-08: removed the `Workspace.task_activity(...)`
  convenience method and routed activity callers through
  `task_activity_store_for_task(...)`. There was no remaining
  `Workspace.append_event(...)` method or caller. The exact `rg` command
  above returns no matches.

  Verification commands:
  - `uv run pytest tests/tasks/test_activity.py tests/cli/test_agent_report.py tests/tasks/test_event_log_rebuild.py tests/lifecycle/test_hooks_and_commit.py tests/lifecycle/test_engine_adapter.py tests/lifecycle/test_recovery_repeat_fingerprint.py -q` (`112 passed, 1 skipped`)
  - `make typecheck` (`0 errors`)
  - `make test` (`940 passed, 1 skipped`)

- [x] P13.3. Remove subagent session convenience methods once callers use
  `WorkspaceSubagents` / `SubagentArtifactStore`.

  Verification:
  - [x] `rg -n "\\.load_subagent_session" litehive tests`

  Completed 2026-05-08: removed `Workspace.load_subagent_session`,
  `Workspace.load_subagent_session_record`, and
  `Workspace.load_subagent_session_created_at`. Production and tests now load
  session rows through `subagent_artifacts(...).load_session_record()`.
  The exact `rg` command above returns no matches.

  Verification commands:
  - `uv run pytest tests/agents/test_session_store.py tests/agents/test_subagent_manager.py tests/recovery/test_runner_recovery.py tests/cli/test_logs.py tests/cli/test_task_debug.py -q` (`52 passed`)
  - `make typecheck` (`0 errors`)
  - `make test` (`940 passed, 1 skipped`)

- [x] P13.4. Confirm `Workspace` only owns identity, config, paths, and DB
  connection.

  Verification:
  - [x] Read `litehive/workspace.py` manually.
  - [x] `make typecheck`
  - [x] `make test`

  Completed 2026-05-08: manually reviewed `litehive/workspace.py`. The
  remaining methods cover identity/equality, boundary construction,
  SQLite connection, config loading, workspace existence/bootstrap, and
  workspace path helpers (`runtime_dir`, `runtime_path`, `control_dir`,
  `control_files`, `task_dir`). Task CRUD, activity, event, and subagent
  artifact/session behavior has moved to focused services/stores.

  Verification commands:
  - `rg -n "^    def |^    @classmethod|TYPE_CHECKING|from litehive\\." litehive/workspace.py`
  - `make typecheck` (`0 errors`)
  - `make test` (`940 passed, 1 skipped`)

## Final Verification

- [x] `make typecheck`
- [x] `make test`
- [x] `make test-integration` if sandbox, CLI round-trips, or engine adapters
  were touched.
- [x] `rg -n "Workspace\\.from_path\\(|root: Path" litehive` reviewed so raw
  root construction remains only at explicit filesystem boundaries.
- [x] `rg -n "temporary wrapper|TODO|compat" litehive docs` reviewed so no
  migration wrapper is forgotten.

Completed 2026-05-08:
- `make typecheck` passed with `0 errors`.
- `make test` passed with `940 passed, 1 skipped`.
- `make test-integration` initially exposed stale real-engine prompts that
  invoked the root `litehive report` command under agent environment variables.
  Updated the integration prompts to use the current
  `litehive agent report` entrypoint, then verified the previously failing
  engine tests (`11 passed`) and the full integration suite (`28 passed`).
- Reviewed `rg -n "Workspace\\.from_path\\(|root: Path" litehive`; remaining
  hits are workspace/config/db/container boundaries, git/path helpers, sandbox
  roots, execution roots, venv-health checkout roots, and daemon log roots.
  Later continuation slices RD2-RD9 cleared the previously documented
  `tasks/paths.py` / `lifecycle/nodes/system.py` raw-root debt.
- Reviewed `rg -n "temporary wrapper|TODO|compat" litehive docs`; later
  continuation slices deleted the execution-trace wrappers, session-store
  loader wrappers, report extraction wrapper, report-storage loader wrapper,
  artifact wrappers, and `Workspace.config()`. The temporary wrapper cleanup
  ledger above has no unchecked concrete wrapper item.

Continuation verification 2026-05-08:
- Rechecked P9-P13 against the current code paths after the wrapper cleanup
  slices. Refreshed stale plan wording for the deleted `WorktreeService`
  facade, deleted `RuntimeStore.create_workspace_state_rows(...)` shim,
  current `AgentReportService.stage_report_from_subagent(...)` method, and
  deleted CLI `run_once(...)` wrapper.
- Verified there are no remaining unchecked checklist items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`.
- Verified focused worktree, state, agent/recovery, daemon/CLI, and workspace
  trim paths with targeted pytest runs while updating each section.
- Verified final state with `make typecheck` (`0 errors`) and `make test`
  (`940 passed, 1 skipped`).
- Re-ran `make test-integration` after confirming the plan had no unchecked
  items; it passed with `27 passed, 1 skipped`.

Continuation verification 2026-05-08, container and raw-root audit:
- Rechecked `litehive/container.py` and CLI/daemon consumers. Production raw
  workspace path conversion is centralized in `build_container(...)`,
  `build_pipeline_container(...)`, `build_daemon_container(...)`, and
  `build_workspace(...)`; CLI runner/app/engine entrypoints pass the resulting
  container/workspace/config/tasks onward instead of rebuilding those
  dependencies internally.
- Re-ran `rg -n "Workspace\\.from_path\\(|root: Path" litehive`; remaining
  hits are explicit filesystem/process boundaries: config path/workspace
  normalization, DB schema/connection, git operations, container assembly,
  daemon log roots, execution roots, venv-health checkout roots, and sandbox
  host roots.
- Verified the container and architecture guardrails with
  `uv run pytest tests/test_container.py tests/test_architecture_guardrails.py tests/cli/test_entrypoint.py tests/lifecycle/test_hook_reject_circuit_breaker.py -q`
  (`41 passed`).

Continuation verification 2026-05-08, CLI boundary audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Audited `litehive/cli` for direct persistence and workspace wiring. CLI
  commands now build containers/workspaces at the command boundary and dispatch
  to workspace-bound services such as `TaskQueueService`, `PoolService`,
  `DaemonExecution`, `WorktreeCleanupService`, `WorktreeRescueService`,
  `TaskReportStore`, `TaskActivityStore`, and `TaskEventLog`.
- Verified no CLI module imports mutating SQLite/state helpers with
  `rg -n "sqlite3|connect_workspace_db|INSERT |UPDATE |DELETE |REPLACE |CREATE |DROP |ALTER |persist_task_and_state|persist_tasks_and_state|save_state_for_workspace|save_task_for_workspace|save_task_runtime_for_workspace" litehive/cli`.
  The only match is `sqlite3.DatabaseError` handling in
  `litehive/cli/task_debug_support.py`.
- Re-ran CLI and architecture guardrail tests:
  `uv run pytest tests/test_architecture_guardrails.py tests/cli -q`
  (`141 passed`).

Continuation verification 2026-05-08, domain-doc cross-check:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Cross-checked `OutcomeReasonCode` values in `litehive/domain/outcomes.py`
  against `docs/domain.md`. Every current enum value is documented in the
  Outcome Reason Code Ownership section, with active setters or explicit
  reserved/no-current-production-setter notes.
- Audited production reason-code setters with
  `rg -n "OutcomeReasonCode\\.[A-Z_]+|reason_code=OutcomeReasonCode\\.[A-Z_]+|outcome_reason_code=OutcomeReasonCode\\.[A-Z_]+" litehive tests tests_integration`.
  Current production setters still match the documented ownership:
  interruption preparation, task abandon, workspace repair/task done,
  hallucinated-completion guard, and lifecycle runtime sync.
- Updated `docs/domain.md` so the subagent boundary names current
  collaborators: `TaskActivityStore` and
  `AgentReportService.stage_report_from_subagent(...)`, not the deleted
  `Workspace.task_activity(...)` and old bare report-extraction function.
- Re-ran focused domain/runtime/close-reason tests:
  `uv run pytest tests/domain/test_pipeline_state.py tests/tasks/test_runtime_updates.py tests/state/test_task_runtime_storage.py tests/cli/test_agent_report.py::test_agent_and_task_close_help_describe_outcome_as_close_reason -q`
  (`49 passed`).

Continuation verification 2026-05-08, deleted wrapper import audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Re-ran the deleted-symbol grep over live code and tests:
  `rg -n "TaskActivityLog|Workspace\\.config\\(|Workspace\\.task_activity|load_subagent_session_record|load_subagent_session\\(|load_subagent_report\\(|load_subagent_event_stream\\(|select_engine_for_workspace|RuntimeStore\\.create_workspace_state_rows|DaemonExecutor|WorktreeService|litehive\\.cli\\.runner\\.run_once|def run_once\\(" litehive tests tests_integration`.
  The only live-code match is the current
  `DaemonExecution.run_once(...)` method in `litehive/daemon/task_execution.py`;
  deleted wrappers, facades, aliases, and compatibility methods remain absent.
- Re-ran the deleted wrapper definition grep:
  `rg -n "def (bootstrap_runtime_settings|load_runtime_settings|apply_runtime_settings_to_config_data|set_runtime_setting|load_runtime_setting_audit_entries|create_task_for_workspace|save_task_for_workspace|mark_task_run_started_for_workspace|select_engine_for_workspace|task_event_log_path|append_task_event|read_task_events|load_task_activity|save_task_activity|append_task_activity|load_subagent_artifacts|load_subagent_session|load_subagent_report|load_subagent_event_stream)\\(" litehive tests tests_integration`;
  it has no matches.
- Re-ran stale-import grep for deleted runtime-settings, engine-selector, and
  task-repository wrapper imports. The only remaining `apply_*` imports are the
  intentionally retained shared in-memory helpers `apply_task_outcome` and
  `clear_task_run_activity`, used by status/recovery transitions that batch
  their own persistence.

Continuation verification 2026-05-08, worktree focused-service audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Audited the remaining worktree cleanup/rescue `*_for_workspace` functions
  after `WorktreeService` deletion. Moved cleanup behavior into
  `WorktreeCleanupService` methods and removed the public
  `cleanup_terminal_task_worktree_for_workspace`,
  `collect_managed_worktrees_for_workspace`, and
  `remove_cleanable_worktrees_for_workspace` functions. Updated the direct
  worktree cleanup test to call `WorktreeCleanupService` directly.
- Removed the public rescue wrapper surface by routing `WorktreeRescueService`
  through private module helpers (`_collect_rescue_candidates`,
  `_require_clean_main_checkout`, and `_apply_rescue_candidate`) instead of
  exported `*_for_workspace` functions. The large cherry-pick algorithm remains
  a private helper behind the service method.
- Verified no live public cleanup/rescue wrapper matches with
  `rg -n "cleanup_terminal_task_worktree_for_workspace|collect_managed_worktrees_for_workspace|remove_cleanable_worktrees_for_workspace|collect_rescue_candidates_for_workspace|require_clean_main_checkout_for_workspace|apply_rescue_candidate_for_workspace\\(" litehive tests tests_integration`.
- Re-ran focused worktree tests:
  `uv run pytest tests/cli/test_worktree_support.py tests/cli/test_worktree_rescue.py tests/cli/test_worktree_clean_with_active_runner.py tests/tasks/test_worktrees.py tests/lifecycle/test_worktree_sync.py -q`
  (`24 passed`) and `make typecheck`.

Continuation verification 2026-05-08, queue mutation wrapper audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Audited queue mutation public wrappers after `TaskQueueService` migration.
  `enqueue_task_for_workspace`, `enqueue_task_front_for_workspace`,
  `move_queued_task_for_workspace`, and
  `prioritize_queued_tasks_for_workspace` had no callers outside
  `litehive/tasks/queue.py`. Removed the back/front enqueue wrappers and made
  move/prioritize private implementation functions behind
  `TaskQueueService`.
- Verified the public wrapper names are absent with
  `rg -n "enqueue_task_for_workspace|enqueue_task_front_for_workspace|move_queued_task_for_workspace|prioritize_queued_tasks_for_workspace" litehive tests tests_integration`;
  remaining matches are only the private `_...` implementation names imported
  by `TaskQueueService`.
- Re-ran focused queue and CLI tests:
  `uv run pytest tests/tasks/test_queue_mutations.py tests/tasks/test_queue_invariants.py tests/tasks/test_zombie_queue_regressions.py tests/cli/test_pool.py tests/cli/test_entrypoint.py -q`
  (`38 passed`) and `make typecheck`.

Continuation verification 2026-05-08, wrapper audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Audited production and test code for the deleted refactor symbols with
  `rg -n "RuntimeStore\\.create_workspace_state_rows|DaemonExecutor|WorktreeService|select_engine_for_workspace|def stage_report_from_subagent\\(" litehive tests tests_integration`.
  A later stale-name cleanup removed the historical
  `select_engine_for_workspace` test-name hit, so the deleted selector name is
  now absent from code and tests. `stage_report_from_subagent` remains present
  only as the current `AgentReportService` method.
- Reviewed broad `temporary wrapper` / `compatibility wrapper` / `shim` /
  `legacy` matches in `litehive`, `tests`, and `tests_integration`; remaining
  hits are current boundary handling, tests, comments, or the sandbox git shim,
  not forgotten wrappers from this object-refactor plan.

Continuation verification 2026-05-08, stale-name cleanup:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- A follow-up deleted-symbol audit found one stale test function name,
  `test_select_engine_for_workspace_records_quota_freeze_and_falls_back`.
  Renamed it to `test_engine_routing_policy_records_quota_freeze_and_falls_back`
  so the test name matches the current `EngineRoutingPolicy` API.
- Verified the deleted selector name is now absent from code/tests with
  `rg -n "select_engine_for_workspace" litehive tests tests_integration`.

Continuation verification 2026-05-08, deleted-symbol audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Re-ran the deleted wrapper/facade symbol audit with
  `rg -n "TaskActivityLog|Workspace\\.config\\(|load_subagent_session_record|load_subagent_session\\(|load_subagent_report\\(|load_subagent_event_stream\\(|stage_report_from_subagent\\(|DaemonExecutor|WorktreeService|select_engine_for_workspace|RuntimeStore\\.create_workspace_state_rows|litehive\\.cli\\.runner\\.run_once" litehive tests tests_integration`.
  The only matches are the current `AgentReportService.stage_report_from_subagent`
  method and its direct callers/tests; deleted wrappers, facades, aliases, and
  selector names remain absent from code and tests.
- Re-ran `make test` after the final documentation cleanup pass; it passed with
  `940 passed, 1 skipped`.

Continuation verification 2026-05-08, raw-root guardrail audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Re-ran `rg -n "Workspace\\.from_path\\(|root: Path" litehive`; remaining
  hits are still explicit filesystem boundaries such as config paths,
  workspace construction, git operations, DB schema, container assembly,
  execution roots, venv-health checkout roots, daemon log roots, and sandbox
  host roots.
- Verified the old internal raw-root debt remains absent with
  `rg -n "def .*\\([^)]*(root|repo_root|workspace_root|main_repo_root): Path" litehive/tasks/paths.py litehive/lifecycle/nodes/system.py`,
  which has no matches.
- Re-ran `uv run pytest tests/test_architecture_guardrails.py -q`; it passed
  with `22 passed`.

Continuation verification 2026-05-08, runtime-settings audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Re-ran the P1 runtime-settings grep. `RuntimeSettingsRepository` and
  `runtime_settings_repository_for_workspace(...)` are the current public API.
  Deleted wrapper names (`bootstrap_runtime_settings`,
  `load_runtime_settings`, `apply_runtime_settings_to_config_data`,
  `set_runtime_setting`, and `load_runtime_setting_audit_entries`) are absent
  from `litehive`, `tests`, and `tests_integration`.
- Updated stale docstring/test-name references from the deleted wrapper names
  to the current repository method/API names.
- Re-ran focused runtime-settings/config tests:
  `uv run pytest tests/config/test_runtime_settings.py tests/config/test_engine_freeze.py tests/config/test_engine_models.py tests/config/test_workspace_bootstrap.py -q`
  (`65 passed`) and `uv run pytest tests/config/test_runtime_settings.py -q`
  (`4 passed`).

Continuation verification 2026-05-08, WorkspaceTasks audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Re-ran the P2 `WorkspaceTasks` grep. `WorkspaceTasks` still exposes the
  task repository methods (`create`, `discard_created`, `list`, `get`,
  `get_record`, `require`, `save`, and runtime helpers), and
  `workspace_tasks_for_workspace(...)` remains the service factory.
- Verified deleted public task wrapper functions and deleted `Workspace`
  task convenience methods remain absent from `litehive`, `tests`, and
  `tests_integration`.
- Re-ran focused task repository/list/debug/event-log tests:
  `uv run pytest tests/state/test_task_repository_characterization.py tests/tasks/test_create_task.py tests/tasks/test_event_log_rebuild.py tests/cli/test_task_list_and_show.py tests/cli/test_task_debug.py -q`;
  it passed with `46 passed`.

Continuation verification 2026-05-08, broad wrapper/compat text audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Re-ran the broad text audit with
  `rg -n "temporary wrapper|TODO|compat|compatibility|shim|legacy" litehive docs tests tests_integration`.
  Remaining matches are historical planning docs, current legacy-boundary
  handling, current tests, compatibility policy text, or the sandbox git shim;
  no untracked object-refactor wrapper cleanup item was found.

Continuation verification 2026-05-08, runtime-store split audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Re-ran the P10 store-split grep against `litehive/state/store.py` and
  `litehive/agents/subagent_ids.py`. `WorkspaceStateStore`, `TaskStateStore`,
  `TaskIntentStore`, `ProcessStateStore`, `RuntimeStore`, and
  `SubagentCounterStore` are present; `RuntimeStore.create_workspace_state_rows`
  remains absent, with only `WorkspaceStateStore.create_workspace_state_rows`
  defined.
- Re-ran focused runtime-store and subagent-counter tests:
  `uv run pytest tests/state/test_runtime_store_characterization.py tests/state/test_db_migrations.py tests/state/test_task_runtime_storage.py tests/agents/test_subagent_ids.py -q`;
  it passed with `44 passed`.

Continuation verification 2026-05-08, subagent boundary audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Re-ran the P11 boundary grep across `litehive/agents`, recovery, roles,
  tasks, and focused tests. `SubagentArtifactStore` owns artifact/session
  loading, `AgentReportService.stage_report_from_subagent(...)` owns report
  extraction, and `SubagentManager` delegates snapshot/session persistence to
  `SubagentSessionManager`; deleted `load_subagent_*` wrappers remain absent.
- Re-ran focused subagent/recovery/engine/debug/report tests:
  `uv run pytest tests/agents tests/recovery tests/lifecycle/test_engine_adapter.py tests/cli/test_task_debug.py tests/cli/test_agent_report.py -q`;
  it passed with `201 passed`.

Continuation verification 2026-05-08, daemon boundary audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Re-ran the P12 daemon grep across `litehive/daemon`, `litehive/cli`, and
  focused tests. `WorkspaceDaemon` owns the daemon loop methods,
  `DaemonExecution.run_once()` is the only daemon run-once method, and the
  deleted `DaemonExecutor` alias plus `litehive.cli.runner.run_once(...)`
  wrapper remain absent.
- Re-ran focused daemon/CLI/lifecycle tests:
  `uv run pytest tests/daemon tests/cli/test_entrypoint.py tests/lifecycle/test_hook_reject_circuit_breaker.py -q`;
  it passed with `32 passed`.

Continuation verification 2026-05-08, workspace trim audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Re-ran the P13 `Workspace` surface grep. `litehive/workspace.py` still only
  exposes identity/equality, boundary construction, SQLite connection, config
  loading, workspace existence/bootstrap, and path helpers. Deleted task,
  activity/event, and subagent-session convenience methods remain absent from
  code and tests.
- Re-ran focused activity/session/event-log/debug tests:
  `uv run pytest tests/tasks/test_activity.py tests/agents/test_session_store.py tests/agents/test_subagent_manager.py tests/recovery/test_runner_recovery.py tests/cli/test_logs.py tests/cli/test_task_debug.py tests/tasks/test_event_log_rebuild.py -q`;
  it passed with `67 passed`.

Continuation verification 2026-05-08, Phase 0 guardrail audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Re-ran the Phase 0 style/guardrail grep. `docs/code-style.md` still contains
  the Dependency Injection section and the tested-slice / temporary-wrapper
  guidance; `tests/test_architecture_guardrails.py` still includes constructor
  and internal raw-root guardrails.
- Re-ran `uv run pytest tests/test_architecture_guardrails.py -q`; it passed
  with `22 passed`.

Continuation verification 2026-05-08, queue selection wrapper audit:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Replaced the remaining service-facing public queue-selection wrappers in
  `litehive/tasks/queue_selection.py` with private implementation functions:
  `_set_active_task`, `_clear_active_task`, `_restore_untouched_active_task`,
  `_active_task_markers_for_workspace`, `_validate_single_active_task_for_workspace`,
  `_peek_next_task`, `_peek_next_task_selection`, `_dequeue_next_task`, and
  `_dequeue_next_task_selection`.
- Updated `TaskQueueService` in `litehive/tasks/queue.py` to call those private
  implementations directly, keeping the queue public API on the service object.
- Verified there are no remaining public definitions with
  `rg -n "^def (set_active_task|clear_active_task|restore_untouched_active_task|active_task_markers_for_workspace|validate_single_active_task_for_workspace|peek_next_task|peek_next_task_selection|dequeue_next_task|dequeue_next_task_selection)\\(" litehive tests tests_integration`.
- Re-ran the broad name grep
  `rg -n "\\b(set_active_task|clear_active_task|restore_untouched_active_task|active_task_markers_for_workspace|validate_single_active_task_for_workspace|peek_next_task|peek_next_task_selection|dequeue_next_task|dequeue_next_task_selection)\\b" litehive tests tests_integration`;
  the only remaining match is the architecture guardrail string for forbidden
  direct task-dequeue calls.
- Re-ran focused queue/CLI tests:
  `uv run pytest tests/tasks/test_queue_mutations.py tests/tasks/test_queue_invariants.py tests/tasks/test_zombie_queue_regressions.py tests/tasks/test_activity.py tests/cli/test_pool.py tests/cli/test_entrypoint.py -q`;
  it passed with `46 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, workspace state lock object migration:
- Rechecked the final workspace-suffixed lock call surface with
  `rg -n "workspace_lock_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`.
- Added `WorkspaceStateLock` in `litehive/state/locking.py` as the
  workspace-bound short blocking `.lock` API, with `hold()` owning the same
  `WorkspaceLockManager` behavior.
- Migrated state persistence, task records, queue mutation/selection, runtime
  transitions, status transitions, runner recovery, workspace repair, and
  worktree rescue to `WorkspaceStateLock(workspace).hold()`.
- Removed `workspace_lock_for_workspace(...)` instead of leaving a shim.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `WorkspaceStateLock` class shape and clarified how it differs from
  `WorkspaceRunnerLock` and `WorkspaceMutationGuard`.
- Verified all workspace-suffixed functions are absent with
  `rg -n "^def .*_for_workspace\\(|^def _.*_for_workspace\\(|workspace_lock_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused lock/state/recovery tests:
  `uv run pytest tests/tasks/test_queue_mutations.py tests/tasks/test_queue_invariants.py tests/tasks/test_runtime_updates.py tests/tasks/test_status_updates.py tests/tasks/test_close_active.py tests/tasks/test_parked_lifecycle.py tests/recovery/test_runner_recovery.py tests/recovery/test_repair.py tests/cli/test_worktree_rescue.py tests/daemon/test_execution.py tests/state/test_process_state.py tests/lifecycle/test_launch_state_recovery.py -q`;
  it passed with `77 passed, 18 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.
- Rechecked open checklist items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it returned no
  matches.

Final verification 2026-05-08:
- Rechecked open checklist items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it returned no
  matches.
- Rechecked removed workspace-first state and lock names with
  `rg -n "^def .*_for_workspace\\(|^def _.*_for_workspace\\(|workspace_lock_for_workspace|load_state_for_workspace|save_state_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Ran the full unit suite with `make test`; it passed with
  `940 passed, 1 skipped, 20 warnings`.

Continuation verification 2026-05-08, workspace state load repository method:
- Rechecked the workspace-state load call surface with
  `rg -n "load_state_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`.
- Migrated production and test callers to
  `WorkspaceStateRepository(workspace).load(...)`, preserving explicit
  `bootstrap=False` call sites.
- Removed `load_state_for_workspace(...)` instead of leaving a shim.
- Fixed malformed nested test calls from the mechanical pass, such as
  `WorkspaceStateRepository(Workspace.from_path(...).load())`, to the correct
  `WorkspaceStateRepository(Workspace.from_path(...)).load()` shape.
- Verified the old load function name is absent with
  `rg -n "load_state_for_workspace|save_state_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran the failed subset after cleanup:
  `uv run pytest tests/config/test_workspace_bootstrap.py tests/tasks/test_flag_auto_defer.py tests/cli/test_worktree_rescue.py tests/cli/test_agent_report.py -q`;
  it passed with `61 passed, 18 warnings`.
- Re-ran broad state/queue/recovery/lifecycle verification:
  `uv run pytest tests/config/test_workspace_bootstrap.py tests/config/test_engine_freeze.py tests/tasks/test_flag_auto_defer.py tests/tasks/test_close_active.py tests/tasks/test_queue_mutations.py tests/tasks/test_status_updates.py tests/tasks/test_runtime_updates.py tests/tasks/test_create_task.py tests/tasks/test_queue_invariants.py tests/tasks/test_zombie_queue_regressions.py tests/tasks/test_audit_log.py tests/tasks/test_parked_lifecycle.py tests/tasks/test_event_log_rebuild.py tests/recovery/test_runner_recovery.py tests/cli/test_worktree_support.py tests/cli/test_worktree_rescue.py tests/lifecycle/test_launch_state_recovery.py tests/lifecycle/test_hooks_and_commit.py tests/observability/test_operator_needed_status.py tests/daemon/test_execution.py -q`;
  it passed with `210 passed, 1 skipped, 16 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, workspace state save repository method:
- Rechecked the workspace-state save call surface with
  `rg -n "save_state_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`.
- Migrated production queue selection, lifecycle worktree crash cleanup, and
  worktree rescue state updates to
  `WorkspaceStateRepository(workspace).save(state)`.
- Migrated focused tests and test helpers to the repository save method.
- Removed `save_state_for_workspace(...)` instead of leaving a shim.
- Verified the old save function name is absent with
  `rg -n "save_state_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused queue/state/status/worktree/recovery tests:
  `uv run pytest tests/tasks/test_queue_mutations.py tests/tasks/test_queue_invariants.py tests/tasks/test_parked_lifecycle.py tests/tasks/test_zombie_queue_regressions.py tests/tasks/test_event_log_rebuild.py tests/tasks/test_audit_log.py tests/tasks/test_create_task.py tests/observability/test_operator_needed_status.py tests/observability/test_status_diagnostics.py tests/cli/test_worktree_support.py tests/cli/test_worktree_rescue.py tests/cli/test_workspace_health.py tests/cli/test_agent_report.py tests/daemon/test_execution.py tests/recovery/test_runner_recovery.py tests/lifecycle/test_launch_state_recovery.py -q`;
  it passed with `171 passed, 22 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task/state persistence repository methods:
- Rechecked the task/state persistence function surface with
  `rg -n "persist_task_and_state_for_workspace|persist_tasks_and_state_for_workspace|persist_tasks_and_state_without_runner_guard_for_workspace|persist_task_and_state_without_runner_guard_for_workspace|merged_state_for_runner_owned_write_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`.
- Added the remaining task/state transactional methods to
  `WorkspaceStateRepository`: `merged_state_for_runner_owned_write(...)`,
  `persist_task_and_state(...)`, `persist_tasks_and_state(...)`,
  `persist_task_and_state_without_runner_guard(...)`, and
  `persist_tasks_and_state_without_runner_guard(...)`.
- Migrated queue selection, runtime transitions, completed-task recovery,
  status transition helpers, stop transitions, lifecycle runtime sync,
  runner recovery, workspace repair, worktree rescue, task creation internals,
  and focused tests to the repository methods.
- Updated the subprocess monkeypatch seam in `tests/tasks/test_create_task.py`
  from the removed module-level merge function to
  `WorkspaceStateRepository.merged_state_for_runner_owned_write(...)`.
- Removed the old module-level task/state persistence functions instead of
  leaving shims.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the full
  repository method set.
- Verified the old persistence function names are absent with
  `rg -n "persist_task_and_state_for_workspace|persist_tasks_and_state_for_workspace|persist_tasks_and_state_without_runner_guard_for_workspace|persist_task_and_state_without_runner_guard_for_workspace|merged_state_for_runner_owned_write_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused task/queue/state/recovery/lifecycle tests:
  `uv run pytest tests/tasks/test_create_task.py tests/tasks/test_runtime_updates.py tests/tasks/test_status_updates.py tests/tasks/test_queue_mutations.py tests/tasks/test_queue_invariants.py tests/state/test_task_persistence.py tests/state/test_task_repository_characterization.py tests/config/test_engine_freeze.py tests/recovery/test_runner_recovery.py tests/recovery/test_repair.py tests/lifecycle/test_launch_state_recovery.py tests/lifecycle/test_hooks_and_commit.py -q`;
  it passed with `142 passed, 1 skipped, 12 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, save-without-runner-guard repository method:
- Rechecked the save-without-runner-guard call surface with
  `rg -n "save_state_without_runner_guard_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`.
- Migrated queue mutations, stale runner recovery, task discard cleanup, and
  focused tests to
  `WorkspaceStateRepository(workspace).save_without_runner_guard(...)`.
- Removed the unused import from `litehive/tasks/queue_mutations.py`.
- Removed `save_state_without_runner_guard_for_workspace(...)` instead of
  leaving a shim.
- Verified the old function name is absent with
  `rg -n "save_state_without_runner_guard_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused queue/runtime/recovery/task repository tests:
  `uv run pytest tests/tasks/test_queue_mutations.py tests/tasks/test_runtime_updates.py tests/tasks/test_create_task.py tests/recovery/test_runner_recovery.py tests/state/test_task_repository_characterization.py -q`;
  it passed with `45 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task completion state repository method:
- Rechecked the narrow completion-counter call surface with
  `rg -n "record_task_completion_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`.
- Moved the consecutive task failure counter and pool-stop trigger into
  `WorkspaceStateRepository.record_task_completion(...)`.
- Removed `record_task_completion_for_workspace(...)` instead of leaving a
  shim.
- Migrated daemon task execution to call
  `WorkspaceStateRepository(workspace).record_task_completion(...)`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the new
  repository method.
- Verified the old completion function name is absent with
  `rg -n "record_task_completion_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused daemon/pool tests:
  `uv run pytest tests/daemon/test_execution.py tests/cli/test_pool.py -q`;
  it passed with `8 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, pool stop state repository method:
- Rechecked the narrow pool-stop call surface with
  `rg -n "set_pool_stop_reason_for_workspace|WorkspaceStateRepository" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`.
- Added `WorkspaceStateRepository` in `litehive/state/persist.py` as the
  workspace-bound policy API over the lower-level SQLite
  `litehive.state.store.WorkspaceStateStore`.
- Moved the pool stop mutation into
  `WorkspaceStateRepository.set_pool_stop_reason(...)` and removed
  `set_pool_stop_reason_for_workspace(...)` instead of leaving a shim.
- Migrated daemon origin-divergence handling, pool service stop handling, and
  operator-needed tests to `WorkspaceStateRepository(workspace)`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `WorkspaceStateRepository` class shape and clarified that it is separate
  from the low-level SQLite `WorkspaceStateStore`.
- Verified the old pool-stop function name is absent with
  `rg -n "set_pool_stop_reason_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused operator/daemon/pool tests:
  `uv run pytest tests/observability/test_operator_needed_status.py tests/daemon/test_execution.py tests/cli/test_pool.py -q`;
  it passed with `16 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task status service boundary:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Added `TaskStatusService` and `task_status_service_for_workspace(...)` in
  `litehive/tasks/status.py` as the workspace-bound object boundary for
  abandon, close, park, requeue, resume, stop, switch-engine, and update
  transitions.
- Migrated production CLI and agent mutation callers to the service boundary:
  `litehive/cli/queue_cli.py`, `litehive/cli/task_cli.py`, and
  `litehive/agents/task_mutation.py`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `TaskStatusService` package/class/method shape.
- Re-ran the direct status-name grep across `litehive/cli`, `litehive/agents`,
  and `litehive/tasks`. Remaining matches are the compatibility shim
  definitions/re-exports in status modules, internal transition delegation
  inside `status_update.py`, internal stop/resume calls inside status/switch
  implementations, and prose comments. The production CLI/agent callers now
  use `task_status_service_for_workspace(...)`.
- Re-ran focused CLI/status/agent mutation tests:
  `uv run pytest tests/cli/test_entrypoint.py tests/cli/test_task_list_and_show.py tests/cli/test_task_logs_support.py tests/tasks/test_status_updates.py tests/agents/test_task_mutation.py -q`;
  it passed with `44 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, status test-import migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Migrated remaining status-focused tests away from direct
  `*_task_for_workspace` imports and onto `TaskStatusService` /
  `task_status_service_for_workspace(...)`:
  `tests/tasks/test_status_updates.py`,
  `tests/tasks/test_create_task.py`,
  `tests/tasks/test_event_log_rebuild.py`,
  `tests/tasks/test_audit_log.py`,
  `tests/tasks/test_flag_auto_defer.py`,
  `tests/tasks/test_parked_lifecycle.py`,
  `tests/lifecycle/test_recovery_repeat_fingerprint.py`,
  `tests/lifecycle/test_rejection_loop_cap.py`, and
  `tests/lifecycle/test_launch_state_recovery.py`.
- Updated the removed-engine signature characterization to inspect
  `TaskStatusService.update` and to verify the bound service method rejects an
  `engine=` keyword at runtime.
- Re-ran the direct test-import grep:
  `rg -n "from litehive\\.tasks\\.status import .*for_workspace|\\b(close_task_for_workspace|park_task_for_workspace|abandon_task_for_workspace|requeue_task_for_workspace|resume_task_for_workspace|update_task_for_workspace)\\b" tests tests_integration`;
  remaining matches are only `task_status_service_for_workspace` or
  `TaskStatusService` imports.
- Re-ran the migrated focused status/lifecycle test set:
  `uv run pytest tests/tasks/test_status_updates.py tests/tasks/test_create_task.py tests/tasks/test_event_log_rebuild.py tests/tasks/test_audit_log.py tests/tasks/test_flag_auto_defer.py tests/tasks/test_parked_lifecycle.py tests/lifecycle/test_recovery_repeat_fingerprint.py tests/lifecycle/test_rejection_loop_cap.py tests/lifecycle/test_launch_state_recovery.py -q`;
  it passed with `70 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, status wrapper deletion:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Deleted the old status wrapper/re-export surface for
  `abandon_task_for_workspace`, `close_task_for_workspace`,
  `park_task_for_workspace`, `requeue_task_for_workspace`,
  `resume_task_for_workspace`, `update_task_for_workspace`,
  `switch_task_engine_for_workspace`, and `stop_current_task`.
- Updated `TaskStatusService` to call private transition implementations
  directly in `status_close.py`, `status_resume.py`, `status_update.py`,
  `stop.py`, and `switch_engine.py`.
- Migrated the remaining engine-switch characterization in
  `tests/config/test_engine_freeze.py` to
  `task_status_service_for_workspace(...).switch_engine(...)`.
- Verified the old status names are absent with
  `rg -n "\\b(abandon_task_for_workspace|close_task_for_workspace|park_task_for_workspace|requeue_task_for_workspace|resume_task_for_workspace|update_task_for_workspace|switch_task_engine_for_workspace|stop_current_task)\\b" litehive tests tests_integration`;
  it returned no matches.
- Re-ran focused status/lifecycle/engine tests:
  `uv run pytest tests/tasks/test_status_updates.py tests/tasks/test_create_task.py tests/tasks/test_event_log_rebuild.py tests/tasks/test_audit_log.py tests/tasks/test_flag_auto_defer.py tests/tasks/test_parked_lifecycle.py tests/lifecycle/test_recovery_repeat_fingerprint.py tests/lifecycle/test_rejection_loop_cap.py tests/lifecycle/test_launch_state_recovery.py tests/config/test_engine_freeze.py -q`;
  it passed with `101 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, completed-task recovery service migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved completed-task recovery behind `TaskStatusService.recover_completed`.
  The former public names `recover_completed_task_for_workspace` and
  `require_completed_task` are now private implementations in
  `litehive/tasks/completed_task_recovery.py`.
- Migrated the production queue recovery command in `litehive/cli/queue_cli.py`
  and the launch-state recovery tests to
  `task_status_service_for_workspace(...).recover_completed(...)`.
- Updated `docs/proposed-object-structure-2026-05-08.md` so
  `TaskStatusService` includes `recover_completed(task_id)` and names
  `litehive/tasks/completed_task_recovery.py` as part of the migration.
- Verified the old public recovery names are absent with
  `rg -n "\\b(recover_completed_task_for_workspace|require_completed_task)\\b" litehive tests tests_integration`;
  it returned no matches.
- Re-ran focused launch-state/CLI/engine tests:
  `uv run pytest tests/lifecycle/test_launch_state_recovery.py tests/cli/test_entrypoint.py tests/config/test_engine_freeze.py -q`;
  it passed with `50 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task logs presenter migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved the workspace-bound `litehive task logs` helper surface behind
  `TaskLogsPresenter` in `litehive/cli/task_logs_support.py`.
- Migrated `litehive/cli/task_cli.py` to dispatch logs branches through
  `task_logs_presenter_for_workspace(...)`.
- Migrated focused tests to the presenter object, including patching
  `TaskLogsPresenter.resolve_follow_task` instead of the deleted module-level
  resolver.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `litehive.cli -> TaskLogsPresenter` package/class/method shape.
- Verified the old public logs helper names are absent with
  `rg -n "show_latest_daemon_log_for_workspace|list_daemon_sessions_for_workspace|show_task_journal_for_workspace|show_latest_subagent_for_workspace|list_task_subagents_for_workspace|follow_active_subagent_for_workspace|resolve_follow_task_for_workspace|load_task_with_runtime_for_workspace" litehive tests tests_integration`;
  it returned no matches.
- Re-ran focused logs/task CLI tests:
  `uv run pytest tests/cli/test_logs.py tests/cli/test_task_logs_support.py tests/cli/test_task_list_and_show.py -q`;
  it passed with `22 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task evidence presenter migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved the workspace-bound `litehive task evidence` / `litehive task debug`
  helper surface behind `TaskEvidencePresenter` in
  `litehive/cli/task_debug_support.py`.
- Migrated `litehive/cli/task_cli.py` evidence/debug branches and
  `TaskLogsPresenter.show_latest_subagent(...)` to use
  `task_evidence_presenter_for_workspace(...)`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `litehive.cli -> TaskEvidencePresenter` package/class/method shape.
- Verified the old public evidence/debug helper names are absent with
  `rg -n "render_task_evidence_for_workspace|debug_all_for_workspace|debug_latest_for_workspace|debug_worktree_for_workspace" litehive tests tests_integration`;
  it returned no matches.
- Re-ran focused debug/logs/task CLI tests:
  `uv run pytest tests/cli/test_task_debug.py tests/cli/test_logs.py tests/cli/test_task_list_and_show.py -q`;
  it passed with `26 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task execution-root resolver migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved task execution-root resolution behind `TaskExecutionRootResolver` in
  `litehive/worktree/execution_root.py`.
- Migrated the focused worktree tests to
  `TaskExecutionRootResolver(...).resolve(task)`.
- Updated `litehive/worktree/__init__.py` and
  `docs/proposed-object-structure-2026-05-08.md` with the resolver object
  shape.
- Verified the old public execution-root helper name is absent with
  `rg -n "resolve_task_execution_root_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused worktree/lifecycle tests:
  `uv run pytest tests/tasks/test_worktrees.py tests/cli/test_worktree_support.py tests/lifecycle/test_persisted_worktree_path.py tests/lifecycle/test_worktree_sync.py -q`;
  it passed with `20 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task execution-root resolver factory removal:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Checked the remaining execution-root factory call sites with
  `rg -n "task_execution_root_resolver_for_workspace|TaskExecutionRootResolver\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  The only code call sites were in `tests/tasks/test_worktrees.py`; production
  already used the `TaskExecutionRootResolver` class directly or did not need
  this factory.
- Removed the now-redundant
  `task_execution_root_resolver_for_workspace(...)` wrapper from
  `litehive/worktree/execution_root.py`.
- Migrated the focused worktree tests to instantiate
  `TaskExecutionRootResolver(Workspace.from_path(...)).resolve(task)` directly.
- Verified the old execution-root factory is absent from code with
  `rg -n "task_execution_root_resolver_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused worktree/lifecycle tests:
  `uv run pytest tests/tasks/test_worktrees.py tests/cli/test_worktree_support.py tests/lifecycle/test_persisted_worktree_path.py tests/lifecycle/test_worktree_sync.py -q`;
  it passed with `20 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, current worktree structure doc cleanup:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Audited the broad worktree path helper surface in `litehive/worktree/paths.py`.
  Those helpers still have broad call sites across worktree sync, cleanup,
  rescue, lifecycle, and tests, and several are pure path/identity helpers, so
  this pass did not force them into a class.
- Removed stale `WorktreeService` facade text from
  `docs/proposed-object-structure-2026-05-08.md`, since
  `litehive/worktree/service.py` is already deleted and current code is split
  across `WorktreeSyncService`, `WorktreeCleanupService`, `WorktreeRescueService`,
  `WorktreeInspector`, and `TaskExecutionRootResolver`.
- Verified the stale `WorktreeService` structure surface is absent from current
  code/tests/structure docs with
  `rg -n "WorktreeService|litehive/worktree/service.py|temporary facade for worktree" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused worktree tests:
  `uv run pytest tests/tasks/test_worktrees.py tests/cli/test_worktree_support.py tests/cli/test_worktree_rescue.py tests/cli/test_worktree_clean_with_active_runner.py tests/lifecycle/test_worktree_sync.py -q`;
  it passed with `24 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task logs presenter factory removal:
- Checked the remaining logs presenter factory call sites with
  `rg -n "task_logs_presenter_for_workspace|TaskLogsPresenter\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  The only code call sites were the CLI dispatcher and one focused support
  test.
- Removed the redundant `task_logs_presenter_for_workspace(...)` wrapper from
  `litehive/cli/task_logs_support.py`.
- Migrated `litehive/cli/task_cli.py` and
  `tests/cli/test_task_logs_support.py` to instantiate
  `TaskLogsPresenter(workspace)` directly.
- Verified the old logs presenter factory is absent from code with
  `rg -n "task_logs_presenter_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused CLI logs tests:
  `uv run pytest tests/cli/test_task_logs_support.py tests/cli/test_logs.py tests/cli/test_task_list_and_show.py -q`;
  it passed with `22 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task evidence presenter factory removal:
- Checked the remaining evidence presenter factory call sites with
  `rg -n "task_evidence_presenter_for_workspace|TaskEvidencePresenter\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  The active code call sites were the task CLI dispatcher and
  `TaskLogsPresenter.show_latest_subagent(...)`.
- Removed the redundant `task_evidence_presenter_for_workspace(...)` wrapper
  from `litehive/cli/task_debug_support.py`.
- Migrated `litehive/cli/task_cli.py` and
  `litehive/cli/task_logs_support.py` to instantiate
  `TaskEvidencePresenter(workspace)` directly.
- Verified the old evidence presenter factory is absent from code with
  `rg -n "task_evidence_presenter_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused CLI evidence/logs tests:
  `uv run pytest tests/cli/test_task_debug.py tests/cli/test_logs.py tests/cli/test_task_list_and_show.py tests/cli/test_task_logs_support.py -q`;
  it passed with `27 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, pool service factory removal:
- Checked the remaining pool service factory call sites with
  `rg -n "pool_service_for_workspace|PoolService\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  The active code call sites were `_run_drain(...)` in `litehive/cli/runner.py`
  and the focused pool CLI tests.
- Removed the redundant `pool_service_for_workspace(...)` wrapper from
  `litehive/cli/pool.py`.
- Migrated `litehive/cli/runner.py` and `tests/cli/test_pool.py` to construct
  `PoolService(...)` directly with the same collaborators.
- Verified the old pool service factory is absent from code with
  `rg -n "pool_service_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused pool/runner tests:
  `uv run pytest tests/cli/test_pool.py tests/cli/test_entrypoint.py -q`;
  it passed with `16 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, agent task mutator builder rename:
- Checked the remaining agent task mutator builder call sites with
  `rg -n "build_agent_task_mutator_for_workspace|AgentTaskMutator\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  The only production caller was `litehive/cli/agent_cli.py`; tests already
  construct `AgentTaskMutator(...)` directly for focused behavior checks.
- Renamed the container helper from `build_agent_task_mutator_for_workspace(...)`
  to `build_agent_task_mutator(...)`, matching the sibling
  `build_agent_report_submitter(...)` helper and avoiding another
  `*_for_workspace` shim name.
- Migrated `litehive/cli/agent_cli.py` to import and call
  `build_agent_task_mutator(...)`.
- Verified the old mutator builder name is absent from code with
  `rg -n "build_agent_task_mutator_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused agent mutation/CLI tests:
  `uv run pytest tests/agents/test_task_mutation.py tests/cli/test_agent_report.py tests/cli/test_entrypoint.py -q`;
  it passed with `51 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, subagent manager builder rename:
- Checked the remaining subagent manager builder call sites with
  `rg -n "build_subagent_manager_for_workspace|SubagentManager\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  The active production callers were `litehive/agents/merge_resolver.py` and
  `litehive/lifecycle/heru_factory.py`; focused agent tests used the same
  container builder for setup.
- Renamed the container helper from `build_subagent_manager_for_workspace(...)`
  to `build_subagent_manager(...)`, keeping subagent wiring centralized in
  `litehive/container.py`.
- Migrated the merge-resolver path, Heru factory path, focused agent tests, and
  `docs/proposed-object-structure-2026-05-08.md` to the new builder name.
- Verified the old subagent manager builder name is absent from code/docs with
  `rg -n "build_subagent_manager_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused subagent/lifecycle tests:
  `uv run pytest tests/agents/test_subagent_manager.py tests/agents/test_subagent_event_stream.py tests/agents/test_stage_report_feedback.py tests/lifecycle/test_hooks_and_commit.py -q`;
  it passed with `68 passed, 1 skipped`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, remaining `*_for_workspace` surface scan:
- Rechecked remaining workspace-suffixed functions with
  `rg -n "^def .*_for_workspace\\(|^def _.*_for_workspace\\(" litehive`.
- Left `runtime_settings_repository_for_workspace` because it wires more than a
  workspace: `RuntimeSettingsRepository` also receives `config_data_loader` and
  `clock`, so replacing the factory should happen through a container-oriented
  dependency slice rather than duplicating that wiring at every call site.
- Left the state persistence and locking functions as the current state API
  surface pending a larger repository split:
  `load_state_for_workspace`, `save_state_for_workspace`,
  `save_state_without_runner_guard_for_workspace`,
  `record_task_completion_for_workspace`, `set_pool_stop_reason_for_workspace`,
  `merged_state_for_runner_owned_write_for_workspace`,
  `persist_task_and_state_for_workspace`,
  `persist_tasks_and_state_for_workspace`,
  `persist_tasks_and_state_without_runner_guard_for_workspace`,
  `persist_task_and_state_without_runner_guard_for_workspace`, and
  `workspace_lock_for_workspace`.
- Left the worktree path helpers in `litehive/worktree/paths.py` as utility
  functions for now: they are pure path/link helpers with broad call paths
  across worktree sync, cleanup, rescue, lifecycle, and tests.

Continuation verification 2026-05-08, status snapshot collector factory removal:
- Checked the remaining status snapshot collector call sites with
  `rg -n "status_snapshot_collector_for_workspace|StatusSnapshotCollector\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  The active production caller was
  `collect_task_pipeline_status_for_workspace(...)`; focused status tests used
  the same factory directly.
- Removed the redundant `status_snapshot_collector_for_workspace(...)` wrapper
  from `litehive/observability/status_diagnostics.py`.
- Migrated `litehive/observability/status.py` and focused observability tests
  to construct `StatusSnapshotCollector(workspace)` directly.
- Updated the status summary monkeypatch seam from the removed factory to
  `StatusSnapshotCollector.collect_operational(...)`.
- Verified the old status collector factory is absent from code/docs with
  `rg -n "status_snapshot_collector_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused status/workspace-health tests:
  `uv run pytest tests/observability/test_status_diagnostics.py tests/observability/test_task_summary.py tests/observability/test_operator_needed_status.py tests/cli/test_workspace_health.py -q`;
  it passed with `62 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, workspace task repository factory removal:
- Rechecked the remaining task repository factory call sites with
  `rg -n "workspace_tasks_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`.
- Verified `WorkspaceTasks.__init__` already owns the default
  `RuntimeStore(workspace)` construction while still accepting an injected
  `RuntimeStore` for tests.
- Migrated the last live production callers in `litehive/container.py` and
  `litehive/attention.py` to direct `WorkspaceTasks(workspace)` construction.
- Verified the old factory name is absent from active code/docs with
  `rg -n "workspace_tasks_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused container/operator/task repository tests:
  `uv run pytest tests/test_container.py tests/observability/test_operator_needed_status.py tests/cli/test_workspace_health.py tests/state/test_task_repository_characterization.py tests/tasks/test_create_task.py tests/tasks/test_queue_mutations.py -q`;
  it passed with `48 passed, 6 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, worktree path service migration:
- Rechecked the remaining worktree path helper call sites with
  `rg -n "task_worktree_path_for_workspace|is_managed_worktree_path_for_workspace|resolve_recorded_worktree_path_for_workspace|ensure_worktree_venv_link_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`.
- Introduced `WorktreePaths` in `litehive/worktree/paths.py` as the
  workspace-bound path policy for managed worktrees.
- Moved the old workspace-first helper behavior into methods:
  `task_worktree_path(...)`, `is_managed_worktree_path(...)`,
  `resolve_recorded_worktree_path(...)`, and `ensure_venv_link(...)`.
- Removed the old worktree path helper functions instead of leaving shims.
- Migrated worktree sync, execution-root resolution, inspection, cleanup,
  rescue, status resume, recovery evidence, Heru execution-root lookup,
  lifecycle worktree setup, and focused tests to `WorktreePaths(workspace)`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `WorktreePaths` class shape.
- Verified the old helper names are absent with
  `rg -n "task_worktree_path_for_workspace|is_managed_worktree_path_for_workspace|resolve_recorded_worktree_path_for_workspace|ensure_worktree_venv_link_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused worktree/lifecycle tests:
  `uv run pytest tests/tasks/test_worktrees.py tests/lifecycle/test_persisted_worktree_path.py tests/lifecycle/test_hooks_and_commit.py tests/lifecycle/test_hook_reject_circuit_breaker.py tests/lifecycle/test_rejection_loop_cap.py tests/cli/test_worktree_rescue.py tests/cli/test_worktree_support.py tests/cli/test_worktree_clean_with_active_runner.py -q`;
  it passed with `60 passed, 1 skipped, 10 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task pipeline status collector migration:
- Checked the remaining pipeline-status function call sites with
  `rg -n "collect_task_pipeline_status_for_workspace|TaskPipelineStatusData|StatusSnapshotCollector\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  The active production callers were `litehive/main.py`,
  `litehive/daemon/execution.py`, and `litehive/cli/workspace.py`.
- Introduced `TaskPipelineStatusCollector` in `litehive/observability/status.py`
  with `collect(read_only=False, diagnostics=False)`.
- Removed the old `collect_task_pipeline_status_for_workspace(...)` function
  instead of leaving a compatibility shim.
- Migrated main status dispatch, daemon status snapshots, workspace status CLI,
  and focused tests to `TaskPipelineStatusCollector(workspace).collect(...)`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the new collector
  object shape.
- Verified the old pipeline-status function is absent from code/docs with
  `rg -n "collect_task_pipeline_status_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused status/daemon/CLI tests:
  `uv run pytest tests/observability/test_task_summary.py tests/observability/test_status_diagnostics.py tests/cli/test_workspace_health.py tests/cli/test_main_entrypoint.py tests/daemon/test_execution.py -q`;
  it passed with `79 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task report store factory removal:
- Checked the remaining report-store factory call sites with
  `rg -n "task_report_store_for_workspace|TaskReportStore\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  The active code call sites already treated the object as the behavior owner
  and only used the factory as a constructor wrapper.
- Removed the redundant `task_report_store_for_workspace(...)` wrapper from
  `litehive/tasks/report_storage.py`.
- Migrated production and test call sites to construct
  `TaskReportStore(workspace)` directly.
- Verified the old report-store factory is absent from code/docs with
  `rg -n "task_report_store_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused report/event/status/lifecycle tests:
  `uv run pytest tests/tasks/test_event_log_rebuild.py tests/state/test_task_runtime_storage.py tests/observability/test_task_summary.py tests/cli/test_workspace_health.py tests/cli/test_task_debug.py tests/cli/test_pool.py tests/lifecycle/test_hooks_and_commit.py tests/lifecycle/test_engine_adapter.py tests/agents/test_subagent_manager.py -q`;
  it passed with `152 passed, 1 skipped`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task event log factory removal:
- Checked the remaining event-log factory call sites with
  `rg -n "task_event_log_for_workspace|TaskEventLog\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  The active code call sites already treated the object as the event-log owner
  and only used the factory as a constructor wrapper.
- Removed the redundant `task_event_log_for_workspace(...)` wrapper from
  `litehive/tasks/event_log.py`.
- Migrated production and test call sites to construct `TaskEventLog(workspace)`
  directly.
- Verified the old event-log factory and accidental duplicate definition are
  absent from code/docs with
  `rg -n "task_event_log_for_workspace|def TaskEventLog\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused event-log/runtime-store/migration tests:
  `uv run pytest tests/tasks/test_event_log_rebuild.py tests/config/test_workspace_bootstrap.py tests/state/test_db_migrations.py tests/state/test_runtime_store_characterization.py tests/state/test_task_runtime_storage.py -q`;
  it passed with `65 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task queue service factory removal:
- Checked the remaining queue service factory call sites with
  `rg -n "task_queue_service_for_workspace|TaskQueueService\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  `TaskQueueService` only needs `workspace`, so callers can construct the
  service directly without spreading extra collaborator wiring.
- Removed the redundant `task_queue_service_for_workspace(...)` wrapper from
  `litehive/tasks/queue.py`.
- Migrated production, integration, and unit test call sites to construct
  `TaskQueueService(workspace)` directly.
- Verified the old queue service factory and accidental duplicate definition
  are absent from code/docs with
  `rg -n "task_queue_service_for_workspace|def TaskQueueService\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused queue/status/runner tests:
  `uv run pytest tests/tasks/test_queue_mutations.py tests/tasks/test_queue_invariants.py tests/tasks/test_zombie_queue_regressions.py tests/tasks/test_close_active.py tests/tasks/test_parked_lifecycle.py tests/tasks/test_status_updates.py tests/agents/test_task_mutation.py tests/lifecycle/test_launch_state_recovery.py tests/lifecycle/test_hooks_and_commit.py tests/cli/test_entrypoint.py tests/cli/test_pool.py -q`;
  it passed with `106 passed, 1 skipped`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task status service factory removal:
- Checked the remaining task status service factory call sites with
  `rg -n "task_status_service_for_workspace|TaskStatusService\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  `TaskStatusService` only needs `workspace`, so callers can construct the
  service directly without spreading extra collaborator wiring.
- Removed the redundant `task_status_service_for_workspace(...)` wrapper from
  `litehive/tasks/status.py`.
- Migrated production and test call sites to construct
  `TaskStatusService(workspace)` directly.
- Verified the old status service factory and accidental duplicate definition
  are absent from code/docs with
  `rg -n "task_status_service_for_workspace|def TaskStatusService\\(|TaskStatusService, TaskStatusService" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused status/queue/CLI tests:
  `uv run pytest tests/tasks/test_status_updates.py tests/tasks/test_flag_auto_defer.py tests/tasks/test_audit_log.py tests/tasks/test_event_log_rebuild.py tests/tasks/test_create_task.py tests/tasks/test_parked_lifecycle.py tests/lifecycle/test_rejection_loop_cap.py tests/lifecycle/test_recovery_repeat_fingerprint.py tests/lifecycle/test_launch_state_recovery.py tests/config/test_engine_freeze.py tests/agents/test_task_mutation.py tests/cli/test_task_list_and_show.py tests/cli/test_entrypoint.py -q`;
  it passed with `132 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, engine routing policy factory removal:
- Checked the remaining engine routing factory call sites with
  `rg -n "engine_routing_policy_for_workspace|EngineRoutingPolicy\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  `EngineRoutingPolicy` takes explicit `workspace` and `config` dependencies,
  so direct construction keeps the same wiring visible at the call site.
- Removed the redundant `engine_routing_policy_for_workspace(...)` wrapper from
  `litehive/config/engine_models.py`.
- Migrated production and test call sites to construct
  `EngineRoutingPolicy(workspace, config)` directly.
- Updated factory monkeypatch seams in tests to patch `EngineRoutingPolicy`
  where the policy is imported.
- Verified the old engine routing factory and accidental duplicate definition
  are absent from code/docs with
  `rg -n "engine_routing_policy_for_workspace|def EngineRoutingPolicy\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused engine/config/lifecycle tests:
  `uv run pytest tests/config/test_engine_freeze.py tests/config/test_engine_models.py tests/config/test_runtime_settings.py tests/cli/test_entrypoint.py tests/lifecycle/test_engine_adapter.py tests/lifecycle/test_rejection_loop_cap.py -q`;
  it passed with `99 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runtime store factory removal:
- Checked the remaining runtime-store factory call sites with
  `rg -n "runtime_store_for_workspace|RuntimeStore\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  `RuntimeStore` only needs `workspace`; the old factory existed primarily as
  a monkeypatch seam and did not add domain behavior.
- Removed the redundant `runtime_store_for_workspace(...)` wrapper from
  `litehive/state/store.py`.
- Migrated production, integration, and unit test call sites to construct
  `RuntimeStore(workspace)` directly.
- Verified the old runtime-store factory and accidental duplicate definition
  are absent from code/docs with
  `rg -n "runtime_store_for_workspace|def RuntimeStore\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused runtime-store/state tests:
  `uv run pytest tests/state/test_runtime_store_characterization.py tests/state/test_task_runtime_storage.py tests/state/test_process_state.py tests/state/test_db_migrations.py tests/state/test_task_persistence.py tests/lifecycle/test_persisted_worktree_path.py tests/lifecycle/test_launch_state_recovery.py tests/cli/test_agent_report.py tests/tasks/test_status_updates.py -q`;
  it passed with `98 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task orchestrator migration:
- Checked the remaining lifecycle orchestration call sites with
  `rg -n "run_task_for_workspace|TaskOrchestrator" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`.
  The active production callers were the root CLI task runner and the
  one-shot/daemon runner seam.
- Introduced `TaskOrchestrator` in `litehive/lifecycle/orchestration.py` with
  `run(task, engine_factory=None, engine_override=None, model_override=None)`.
- Removed the old `run_task_for_workspace(...)` function instead of leaving a
  compatibility shim.
- Migrated production, unit, and integration call sites to construct
  `TaskOrchestrator(workspace, config).run(...)`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the orchestrator
  object shape.
- Verified the old lifecycle function is absent from code/docs with
  `rg -n "run_task_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused lifecycle/CLI/integration tests:
  `uv run pytest tests/lifecycle/test_hooks_and_commit.py tests/lifecycle/test_rejection_loop_cap.py tests/lifecycle/test_recovery_repeat_fingerprint.py tests/lifecycle/test_launch_state_recovery.py tests/lifecycle/test_hook_reject_circuit_breaker.py tests/tasks/test_parked_lifecycle.py tests/tasks/test_zombie_queue_regressions.py tests/cli/test_entrypoint.py tests/cli/test_pool.py tests_integration/lifecycle/test_bootstrap.py tests_integration/lifecycle/test_end_to_end.py -q`;
  it passed with `94 passed, 1 skipped`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task runtime transitions factory removal:
- Checked the remaining runtime-transition factory call sites with
  `rg -n "task_runtime_transitions_for_workspace|TaskRuntimeTransitions\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
  `TaskRuntimeTransitions` requires `workspace` plus `WorkspaceTasks`; callers
  already had the workspace and now pass
  `workspace_tasks_for_workspace(workspace)` explicitly.
- Removed the redundant `task_runtime_transitions_for_workspace(...)` wrapper
  from `litehive/tasks/runtime.py`.
- Migrated production and test call sites to construct
  `TaskRuntimeTransitions(workspace, workspace_tasks_for_workspace(workspace))`
  directly.
- Verified the old runtime-transition factory and accidental duplicate
  definition are absent from code/docs with
  `rg -n "task_runtime_transitions_for_workspace|def TaskRuntimeTransitions\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused runtime/subagent/status tests:
  `uv run pytest tests/tasks/test_runtime_updates.py tests/tasks/test_close_active.py tests/tasks/test_flag_auto_defer.py tests/agents/test_subagent_event_stream.py tests/agents/test_subagent_manager.py tests/tasks/test_status_updates.py tests/config/test_engine_freeze.py -q`;
  it passed with `91 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runtime settings repository factory removal:
- Checked the remaining runtime-settings factory call sites with
  `rg -n "runtime_settings_repository_for_workspace|RuntimeSettingsRepository\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`.
- Moved the default `WorkspaceConfigLoader(...).effective_data()` and `utcnow`
  wiring into optional defaults on `RuntimeSettingsRepository.__init__(...)`.
- Removed the redundant `runtime_settings_repository_for_workspace(...)`
  wrapper from `litehive/config/runtime_settings.py`.
- Migrated production and test call sites to construct
  `RuntimeSettingsRepository(workspace)` directly.
- Verified the old runtime-settings factory and accidental duplicate definition
  are absent from code/docs with
  `rg -n "runtime_settings_repository_for_workspace|def RuntimeSettingsRepository\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused config/runtime-settings tests:
  `uv run pytest tests/config/test_runtime_settings.py tests/config/test_engine_freeze.py tests/config/test_loading.py tests/config/test_workspace_loading.py tests/cli/test_entrypoint.py -q`;
  it passed with `75 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, worktree dirty-gate inspector migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved dirty-worktree gate inspection into `WorktreeInspector.inspect_dirty_gate()`
  and moved committed-ahead-of-main path collection into
  `WorktreeInspector.committed_changes(...)`.
- Migrated `litehive/cli/workspace.py` and
  `tests/tasks/test_parked_lifecycle.py` to the `WorktreeInspector` methods.
- Updated `docs/proposed-object-structure-2026-05-08.md` so
  `WorktreeInspector` lists `inspect_dirty_gate()` and `committed_changes(...)`.
- Verified the old public names are absent with
  `rg -n "inspect_dirty_worktree_gate|worktree_committed_changes_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused worktree/health/status tests:
  `uv run pytest tests/tasks/test_parked_lifecycle.py tests/cli/test_workspace_health.py tests/cli/test_worktree_support.py tests/observability/test_operator_needed_status.py -q`;
  it passed with `34 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, workspace health presenter migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved daemon health-row lookup from the free
  `health_daemon_status_for_workspace(...)` helper into
  `WorkspaceHealthPresenter.daemon_status()`.
- Replaced the positional `(status, pid)` tuple with the named
  `DaemonHealthStatus` dataclass.
- Migrated `health_command(...)` and `tests/cli/test_workspace_health.py` to
  the presenter method and removed the old helper instead of keeping a shim.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `WorkspaceHealthPresenter` package/class/method shape.
- Verified the old public helper name is absent with
  `rg -n "health_daemon_status_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused health tests:
  `uv run pytest tests/cli/test_workspace_health.py -q`;
  it passed with `12 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, operator attention projector migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved operator-needed state projection and waiting-line rendering from
  `collect_operator_needed_state_for_workspace(...)` and
  `waiting_for_you_lines_for_workspace(...)` into
  `OperatorAttentionProjector.collect_state()` and
  `OperatorAttentionProjector.waiting_lines()`.
- Migrated `litehive/observability/status.py` to instantiate the projector at
  the status boundary, preserving the read-only and normal waiting-line paths.
- Migrated `tests/observability/test_operator_needed_status.py` and
  `tests/observability/test_task_summary.py` to the projector method surface.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `litehive.attention -> OperatorAttentionProjector` class shape.
- Verified the old public helper names are absent with
  `rg -n "collect_operator_needed_state_for_workspace|waiting_for_you_lines_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused observability tests:
  `uv run pytest tests/observability/test_operator_needed_status.py tests/observability/test_task_summary.py -q`;
  it passed with `20 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task artifact locator migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved the workspace-bound artifact lookup helpers
  `latest_run_all_log_path_for_workspace(...)` and
  `latest_subagent_base_for_workspace(...)` into
  `TaskArtifactLocator.latest_run_all_log_path()` and
  `TaskArtifactLocator.latest_subagent_base(task)`.
- Migrated `litehive/tasks/recovery_evidence.py`,
  `litehive/tasks/switch_engine.py`, and `litehive/roles/recovery.py` to the
  locator object and removed the old helper functions instead of keeping
  shims.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `litehive.tasks -> TaskArtifactLocator` class shape.
- Verified the old public helper names are absent with
  `rg -n "latest_run_all_log_path_for_workspace|latest_subagent_base_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused engine-switch and recovery-prompt tests:
  `uv run pytest tests/config/test_engine_freeze.py::test_switch_task_engine_accepts_injected_workspace tests/lifecycle/test_prompt_serializer.py::test_serialize_recovery_inlines_failed_subagent_diagnostics -q`;
  it passed with `2 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner recovery service migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved stale runner recovery from
  `recover_stale_runner_state_for_workspace(...)` into
  `RunnerRecoveryService.recover_stale_runner_state(...)`.
- Migrated queue viewing/selection, task stop, workspace repair, queue CLI,
  launch-state recovery tests, and recovery tests to the bound service.
- Removed the old recovery helper instead of keeping a shim.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `litehive.recovery -> RunnerRecoveryService` class shape.
- Verified the old public helper name is absent with
  `rg -n "recover_stale_runner_state_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused recovery/queue/stop tests:
  `uv run pytest tests/recovery/test_repair.py tests/recovery/test_runner_recovery.py tests/lifecycle/test_launch_state_recovery.py tests/tasks/test_queue_invariants.py tests/tasks/test_close_active.py -q`;
  it passed with `29 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, daemon logs latest-dir shim deletion:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Deleted the `latest_run_all_log_dir_for_workspace(...)` wrapper from
  `litehive/daemon/logs.py`; callers now use
  `DaemonLogs(workspace).latest_run_all_dir()` directly.
- Migrated `TaskLogsPresenter.show_latest_daemon_log(...)`,
  `_probe_last_cycle_for_workspace(...)`, and
  `daemon_status_lines_for_workspace(...)` to the `DaemonLogs` method.
- Verified the old wrapper name is absent with
  `rg -n "latest_run_all_log_dir_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused logs/status/daemon tests:
  `uv run pytest tests/cli/test_logs.py tests/cli/test_task_logs_support.py tests/observability/test_status_diagnostics.py tests/daemon/test_execution.py -q`;
  it passed with `47 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task dependency validator migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved dependency validation from the public
  `validate_task_dependencies_for_workspace(...)` helper into
  `TaskDependencyValidator.validate(...)`.
- Added `TaskQueueService.validate_dependencies(...)` as the queue-service
  method surface for status updates.
- Migrated task creation in `litehive/state/records.py` to use the validator
  directly at the persistence boundary and migrated
  `litehive/tasks/status_update.py` to use the queue service method.
- Removed the old public helper instead of keeping a shim.
- Updated `docs/proposed-object-structure-2026-05-08.md` with
  `TaskDependencyValidator` and the new `TaskQueueService` method.
- Verified the old public helper name is absent with
  `rg -n "validate_task_dependencies_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused create/update/queue tests:
  `uv run pytest tests/tasks/test_create_task.py tests/tasks/test_queue_invariants.py tests/tasks/test_status_updates.py -q`;
  it passed with `43 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, workspace backup service migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved workspace database backup listing, pruning, creation, scheduled
  creation, and restore into `WorkspaceBackupService`.
- Migrated backup CLI commands, daemon scheduled-backup hook, and backup tests
  to `WorkspaceBackupService`.
- Removed the old public backup helper functions instead of keeping shims:
  `create_workspace_backup_for_workspace`,
  `list_workspace_backups_for_workspace`,
  `prune_workspace_backups_for_workspace`,
  `create_scheduled_workspace_backup_for_workspace`, and
  `restore_workspace_backup_for_workspace`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `WorkspaceBackupService` class shape.
- Verified the old public helper names are absent with
  `rg -n "create_workspace_backup_for_workspace|list_workspace_backups_for_workspace|prune_workspace_backups_for_workspace|create_scheduled_workspace_backup_for_workspace|restore_workspace_backup_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused backup/daemon tests:
  `uv run pytest tests/state/test_backups.py tests/daemon/test_execution.py -q`;
  it passed with `11 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, workspace venv health migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved venv discovery, broken-executable probing, and daemon startup gating
  into `WorkspaceVenvHealth`.
- Migrated daemon startup to `WorkspaceVenvHealth(workspace).ensure_ready()`
  and updated daemon tests to patch the new method surface.
- Removed the old helper names instead of keeping shims:
  `discover_workspace_venvs_for_workspace`,
  `probe_broken_venv_executables_for_workspace`, and
  `create_workspace_venvs_ready_for_workspace`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `WorkspaceVenvHealth` class shape.
- Verified the old helper names are absent with
  `rg -n "create_workspace_venvs_ready_for_workspace|probe_broken_venv_executables_for_workspace|discover_workspace_venvs_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused daemon tests:
  `uv run pytest tests/daemon/test_registry.py tests/daemon/test_execution.py -q`;
  it passed with `14 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, engine freeze wrapper deletion:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Deleted the old engine-freeze persistence wrappers
  `persist_engine_freeze_iso_for_workspace(...)` and
  `clear_persisted_engine_freeze_for_workspace(...)`.
- Routed `EngineRoutingPolicy.freeze(...)`, `EngineRoutingPolicy.unfreeze(...)`,
  and the quota-driven freeze/clear internals directly through
  `set_engine_freeze(...)` and `clear_engine_freeze(...)`.
- Migrated focused engine-freeze tests to the `EngineRoutingPolicy` method
  surface.
- Verified the old public helper names are absent with
  `rg -n "persist_engine_freeze_iso_for_workspace|clear_persisted_engine_freeze_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused engine-freeze tests:
  `uv run pytest tests/config/test_engine_freeze.py -q`;
  it passed with `31 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, workspace config loader migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved config data loading, validated config loading, and context loading
  into `WorkspaceConfigLoader.effective_data()`, `WorkspaceConfigLoader.load()`,
  and `WorkspaceConfigLoader.context()`.
- Migrated `Workspace.config()`, runtime settings repository wiring, and
  focused config/lifecycle tests to the loader object.
- Removed the old public loader helper names instead of keeping shims:
  `load_effective_config_data_for_workspace`,
  `load_config_for_workspace`, and `load_context_for_workspace`.
- Updated `docs/proposed-object-structure-2026-05-08.md` so
  `WorkspaceConfigLoader` lists the current method names.
- Verified the old public helper names are absent with
  `rg -n "load_config_for_workspace|load_context_for_workspace|load_effective_config_data_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused config/lifecycle tests:
  `uv run pytest tests/config/test_loading.py tests/config/test_workspace_loading.py tests/config/test_workspace_bootstrap.py tests/config/test_engine_models.py tests/config/test_engine_freeze.py tests/config/test_claude_settings.py tests/config/test_runner_hooks.py tests/lifecycle/test_prompt_serializer.py tests/lifecycle/test_engine_adapter.py -q`;
  it passed with `173 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, database rebuild safety migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved workspace-bound rebuild-safety artifact discovery, replay coverage,
  destructive-rebuild assertion, and pre-rebuild backup into
  `DatabaseRebuildSafety`.
- Kept `sqlite_task_ids(db_path)` as a pure path helper because it only
  inspects a supplied database path.
- Migrated task event-log replay and migration-triggered rebuild paths to
  `DatabaseRebuildSafety`.
- Removed the old public helper names instead of keeping shims:
  `task_artifact_dir_ids_for_workspace`,
  `event_log_replay_task_ids_for_workspace`,
  `assert_database_rebuild_safe_for_workspace`, and
  `backup_database_before_rebuild_for_workspace`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `DatabaseRebuildSafety` class shape.
- Verified the old public helper names are absent with
  `rg -n "task_artifact_dir_ids_for_workspace|event_log_replay_task_ids_for_workspace|assert_database_rebuild_safe_for_workspace|backup_database_before_rebuild_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused DB migration/event-log rebuild tests:
  `uv run pytest tests/state/test_db_migrations.py tests/tasks/test_event_log_rebuild.py -q`;
  it passed with `18 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, remaining WorkspaceTasks helper migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved follow-up task creation into `WorkspaceTasks.create_follow_ups(...)`.
- Moved state-priority task listing into `WorkspaceTasks.list_state_first(...)`.
- Migrated workspace status rendering and focused task creation tests to the
  `WorkspaceTasks` methods.
- Removed the old public helper names instead of keeping shims:
  `create_follow_up_tasks_for_workspace(...)` and
  `list_tasks_state_first_for_workspace(...)`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the new
  `WorkspaceTasks` methods.
- Verified the old public helper names are absent with
  `rg -n "create_follow_up_tasks_for_workspace|list_tasks_state_first_for_workspace|list_tasks_state_first\\b" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md docs/object-refactor-plan-2026-05-08.md`;
  it returned no matches.
- Re-ran focused task creation/workspace health tests:
  `uv run pytest tests/tasks/test_create_task.py tests/cli/test_workspace_health.py -q`;
  it passed with `32 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, daemon status presenter migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved the workspace-bound daemon status rendering from the public
  `daemon_status_lines_for_workspace(...)` helper into
  `DaemonStatusPresenter.status_lines()`.
- Kept `daemon_status_lines(workspace: Path)` as the CLI boundary function
  because `litehive/cli/runner.py` still receives a path from the command
  surface.
- Updated `docs/proposed-object-structure-2026-05-08.md` with
  `DaemonStatusPresenter`.
- Verified the old public helper name is absent with
  `rg -n "daemon_status_lines_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused daemon/CLI tests:
  `uv run pytest tests/daemon/test_execution.py tests/daemon/test_registry.py tests/cli/test_entrypoint.py -q`;
  it passed with `28 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, daemon status snapshot collector migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved daemon-loop read-only pipeline snapshot collection from the private
  `_daemon_status_snapshot_for_workspace(...)` helper into
  `DaemonStatusSnapshotCollector.snapshot()`.
- Migrated `WorkspaceDaemon.run_cycle(...)` and the focused daemon snapshot
  characterization test to the collector method.
- Removed the old private helper instead of keeping a shim.
- Updated `docs/proposed-object-structure-2026-05-08.md` with
  `DaemonStatusSnapshotCollector`.
- Verified the old helper name is absent with
  `rg -n "_daemon_status_snapshot_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused daemon/CLI tests:
  `uv run pytest tests/daemon/test_execution.py tests/daemon/test_registry.py tests/cli/test_entrypoint.py -q`;
  it passed with `28 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner lock pid-stale method migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Introduced `WorkspaceRunnerLock` in `litehive/state/locking.py` as the
  workspace-bound runner lock object described by the proposed structure doc.
- Moved PID-stale probing from the old
  `runner_lock_pid_is_stale_for_workspace(...)` wrapper to
  `WorkspaceRunnerLock.pid_is_stale()`.
- Migrated the stale-running-task recovery gate to
  `WorkspaceRunnerLock(workspace).pid_is_stale()`.
- Removed the old public helper instead of keeping a shim.
- Verified the old helper name is absent with
  `rg -n "runner_lock_pid_is_stale_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused recovery/state/task tests:
  `uv run pytest tests/recovery/test_repair.py tests/recovery/test_runner_recovery.py tests/state/test_process_state.py tests/tasks/test_close_active.py -q`;
  it passed with `15 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner lock active/clear method migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved runner-lock active probing from
  `runner_lock_is_active_for_workspace(...)` to
  `WorkspaceRunnerLock.is_active()`.
- Moved stale lockfile metadata cleanup from
  `clear_runner_lock_metadata_for_workspace(...)` to
  `WorkspaceRunnerLock.clear_metadata()`.
- Migrated `runner_status_for_workspace(...)` to use those object methods and
  removed both wrappers instead of keeping shims.
- Verified the old helper names are absent with
  `rg -n "runner_lock_is_active_for_workspace|clear_runner_lock_metadata_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused runner-status, backup, daemon, and recovery tests:
  `uv run pytest tests/state/test_process_state.py tests/state/test_backups.py tests/daemon/test_execution.py tests/recovery/test_repair.py tests/recovery/test_runner_recovery.py -q`;
  it passed with `24 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner lock status method migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved authoritative runner-status resolution from
  `runner_status_for_workspace(...)` to `WorkspaceRunnerLock.status()`.
- Migrated daemon run-cycle waiting, daemon status rendering, backup restore
  safety checks, and focused monkeypatch tests to `WorkspaceRunnerLock`.
- Removed the old public helper instead of keeping a shim.
- Verified the exact old helper name is absent with
  `rg -n "(^|[^A-Za-z0-9_])runner_status_for_workspace\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused daemon, backup, process-state, and recovery tests:
  `uv run pytest tests/daemon/test_execution.py tests/state/test_backups.py tests/state/test_process_state.py tests/recovery/test_repair.py tests/recovery/test_runner_recovery.py -q`;
  it passed with `24 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner lock metadata/held method migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved lockfile metadata reads from
  `read_runner_lock_metadata_for_workspace(...)` to
  `WorkspaceRunnerLock.read_metadata()`.
- Moved held-lock probing from `runner_lock_is_held_for_workspace(...)` to
  `WorkspaceRunnerLock.is_held()`.
- Migrated task stop, task close, stale-runner recovery, and runner-lock
  subprocess tests to the object methods.
- Removed both old public helpers instead of keeping shims.
- Verified the old helper names are absent with
  `rg -n "read_runner_lock_metadata_for_workspace|runner_lock_is_held_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused task stop/close, worktree rescue, and recovery tests:
  `uv run pytest tests/tasks/test_close_active.py tests/cli/test_worktree_rescue.py tests/recovery/test_repair.py tests/recovery/test_runner_recovery.py tests/tasks/test_status_updates.py -q`;
  it passed with `30 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner lock current-thread ownership migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved reentrant runner-guard ownership detection from
  `current_thread_owns_runner_guard_for_workspace(...)` to
  `WorkspaceRunnerLock.owns_current_thread()`.
- Migrated workspace mutation guard internals, stale-runner recovery gates,
  and task status updates to the object method.
- Updated `WorkspaceRunnerLock.is_held()` to use the object method as its
  in-process ownership callback.
- Removed the old helper instead of keeping a shim.
- Updated `docs/proposed-object-structure-2026-05-08.md` with
  `owns_current_thread()`.
- Verified the old helper name is absent with
  `rg -n "current_thread_owns_runner_guard_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused recovery/status/process-state tests:
  `uv run pytest tests/recovery/test_repair.py tests/recovery/test_runner_recovery.py tests/tasks/test_status_updates.py tests/tasks/test_close_active.py tests/state/test_process_state.py -q`;
  it passed with `27 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner lock touch method migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved runner heartbeat metadata refresh from
  `touch_runner_status_for_workspace(...)` to
  `WorkspaceRunnerLock.touch(...)`.
- Migrated the runner heartbeat context and process-state test to the object
  method.
- Removed the old helper instead of keeping a shim.
- Updated `docs/proposed-object-structure-2026-05-08.md` with
  `touch(active_task_id=None)`.
- Verified the old helper name is absent with
  `rg -n "touch_runner_status_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused process-state, runner-lock subprocess, worktree rescue, and
  lifecycle tests:
  `uv run pytest tests/state/test_process_state.py tests/tasks/test_close_active.py tests/cli/test_worktree_rescue.py tests/lifecycle/test_hooks_and_commit.py -q`;
  it passed with `42 passed, 1 skipped`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner lock heartbeat method migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved runner heartbeat context management from
  `runner_heartbeat_for_workspace(...)` to
  `WorkspaceRunnerLock.heartbeat(...)`.
- Migrated lifecycle orchestration and the fake-runner subprocess scripts in
  close/rescue tests to the object method.
- Removed the old helper instead of keeping a shim.
- Updated `docs/proposed-object-structure-2026-05-08.md` with
  `heartbeat(active_task_id=None, interval_seconds=1.0)`.
- Verified the old helper name is absent with
  `rg -n "runner_heartbeat_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused process-state, runner-lock subprocess, worktree rescue, and
  lifecycle tests:
  `uv run pytest tests/state/test_process_state.py tests/tasks/test_close_active.py tests/cli/test_worktree_rescue.py tests/lifecycle/test_hooks_and_commit.py tests/lifecycle/test_launch_state_recovery.py -q`;
  it passed with `47 passed, 1 skipped`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner lock conflict-message migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved runner lock conflict-message rendering from
  `runner_conflict_message_for_workspace(...)` to
  `WorkspaceRunnerLock.conflict_message()`.
- Migrated `workspace_runner_guard(...)` conflict branches to the object
  method.
- Removed the old helper instead of keeping a shim, and cleaned the stale
  docstring reference to the deleted helper name.
- Verified the old helper name is absent with
  `rg -n "runner_conflict_message_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused process-state, runner-lock subprocess, worktree rescue, and
  recovery tests:
  `uv run pytest tests/state/test_process_state.py tests/tasks/test_close_active.py tests/cli/test_worktree_rescue.py tests/recovery/test_repair.py -q`;
  it passed with `11 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner lock guard method migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved long-lived runner guard behavior from
  `workspace_runner_guard(...)` to `WorkspaceRunnerLock.guard()`.
- Migrated lifecycle orchestration, workspace mutation guard internals,
  process-state tests, and fake-runner subprocess scripts to the object
  method.
- Removed the old guard helper instead of keeping a shim.
- Updated `docs/proposed-object-structure-2026-05-08.md` to list `guard()`.
- Verified the old helper name is absent with
  `rg -n "workspace_runner_guard\\(" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused process-state, runner-lock subprocess, worktree rescue,
  lifecycle, and recovery tests:
  `uv run pytest tests/state/test_process_state.py tests/tasks/test_close_active.py tests/cli/test_worktree_rescue.py tests/lifecycle/test_hooks_and_commit.py tests/lifecycle/test_launch_state_recovery.py tests/recovery/test_repair.py -q`;
  it passed with `49 passed, 1 skipped`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner lock reconciliation method migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved stale runner-state reconciliation detection from
  `runner_status_needs_reconciliation_for_workspace(...)` to
  `WorkspaceRunnerLock.needs_reconciliation()`.
- Updated `WorkspaceRunnerLock.status()` to call the object method.
- Removed the old helper instead of keeping a shim.
- Verified the old helper name is absent with
  `rg -n "runner_status_needs_reconciliation_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused status/recovery tests:
  `uv run pytest tests/state/test_process_state.py tests/state/test_backups.py tests/recovery/test_repair.py tests/recovery/test_runner_recovery.py -q`;
  it passed with `18 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner lock process-state private helper migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved the runner SQLite process-state mirror writes from private
  `_save_runner_process_state_for_workspace(...)` and
  `_clear_runner_process_state_for_workspace(...)` helpers into private
  `WorkspaceRunnerLock._save_process_state(...)` and
  `WorkspaceRunnerLock._clear_process_state()` methods.
- Updated `WorkspaceRunnerLock.clear_metadata()`, `touch()`, and `guard()` to
  use those methods.
- Removed both private workspace helpers instead of keeping shims.
- Verified the old helper names are absent with
  `rg -n "_save_runner_process_state_for_workspace|_clear_runner_process_state_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused process-state and runner-lock subprocess tests:
  `uv run pytest tests/state/test_process_state.py tests/tasks/test_close_active.py tests/cli/test_worktree_rescue.py -q`;
  it passed with `9 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, workspace mutation guard migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Introduced `WorkspaceMutationGuard` with `hold()` and
  `is_owned_by_current_thread()` for the short mutation-guard concern already
  described in the proposed object structure.
- Moved `workspace_mutation_guard_for_workspace(...)` behavior into
  `WorkspaceMutationGuard.hold()`.
- Migrated state persistence, task records, queue selection, runtime
  transitions, completed-task recovery, and engine monitoring to the object
  method.
- Removed the old wrapper instead of keeping a shim.
- Verified the old wrapper name is absent with
  `rg -n "workspace_mutation_guard_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused state/task/queue/observability tests:
  `uv run pytest tests/state/test_task_persistence.py tests/state/test_process_state.py tests/tasks/test_queue_invariants.py tests/tasks/test_runtime_updates.py tests/tasks/test_status_updates.py tests/tasks/test_create_task.py tests/tasks/test_activity.py tests/observability/test_status_diagnostics.py -q`;
  it passed with `95 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, future task mutation guard migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved active-runner conflict checks for future task edits from
  `ensure_future_task_mutation_allowed_for_workspace(...)` to
  `WorkspaceMutationGuard.ensure_future_task_mutation_allowed(...)`.
- Migrated queue mutations, task status close/resume/update flows, and
  worktree rescue finalization to the object method.
- Removed the old helper instead of keeping a shim.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the new
  `WorkspaceMutationGuard` method.
- Verified the old helper name is absent with
  `rg -n "ensure_future_task_mutation_allowed_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused queue/status/worktree tests:
  `uv run pytest tests/tasks/test_queue_invariants.py tests/tasks/test_status_updates.py tests/tasks/test_runtime_updates.py tests/tasks/test_close_active.py tests/cli/test_worktree_rescue.py tests/tasks/test_create_task.py -q`;
  it passed with `59 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, future task update persistence migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved single-task future edit persistence from
  `persist_future_task_update_for_workspace(...)` to
  `WorkspaceMutationGuard.persist_future_task_update(...)`.
- Migrated task metadata updates and lifecycle runtime sync to the object
  method.
- Removed the old helper instead of keeping a shim.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the new
  `WorkspaceMutationGuard` method.
- Verified the old helper name is absent with
  `rg -n "persist_future_task_update_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused status/runtime/lifecycle tests:
  `uv run pytest tests/tasks/test_status_updates.py tests/tasks/test_runtime_updates.py tests/lifecycle/test_hooks_and_commit.py tests/lifecycle/test_launch_state_recovery.py tests/lifecycle/test_worktree_sync.py -q`;
  it passed with `67 passed, 1 skipped`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, queue mutation service method cleanup:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved enqueue, move, and prioritize implementation bodies from private
  `_enqueue_task_for_workspace(...)`,
  `_move_queued_task_for_workspace(...)`, and
  `_prioritize_queued_tasks_for_workspace(...)` helpers into
  `TaskQueueService.enqueue(...)`, `TaskQueueService.move(...)`, and
  `TaskQueueService.prioritize(...)`.
- Kept `_prioritize_audit_entries(...)` as a pure audit-entry helper because it
  does not bind workspace state or acquire locks.
- Removed the three old private workspace helpers instead of keeping shims.
- Verified the old helper names are absent with
  `rg -n "_enqueue_task_for_workspace|_move_queued_task_for_workspace|_prioritize_queued_tasks_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused queue/CLI tests:
  `uv run pytest tests/tasks/test_queue_mutations.py tests/tasks/test_queue_invariants.py tests/cli/test_entrypoint.py -q`;
  it passed with `29 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, queue active-marker helper cleanup:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Removed the remaining workspace-first active-marker helper names
  `_active_task_markers_for_workspace(...)` and
  `_validate_single_active_task_for_workspace(...)`.
- Renamed their implementation bodies to local `_active_task_markers_impl(...)`
  and `_validate_single_active_task_impl(...)`, and kept
  `TaskQueueService.active_task_markers(...)` plus
  `TaskQueueService.validate_single_active_task(...)` as the public object
  surface.
- Verified the old helper names are absent with
  `rg -n "_active_task_markers_for_workspace|_validate_single_active_task_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused queue/status/recovery tests:
  `uv run pytest tests/tasks/test_queue_invariants.py tests/tasks/test_close_active.py tests/recovery/test_repair.py tests/tasks/test_status_updates.py -q`;
  it passed with `27 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task switch-engine implementation helper cleanup:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Removed the remaining workspace-first switch-engine implementation name
  `_switch_task_engine_for_workspace(...)`.
- Renamed the body to local `_switch_task_engine_impl(...)`, keeping
  `TaskStatusService.switch_engine(...)` as the object method surface.
- Verified the old helper name is absent with
  `rg -n "_switch_task_engine_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused switch/status/CLI tests:
  `uv run pytest tests/config/test_engine_freeze.py::test_switch_task_engine_accepts_injected_workspace tests/tasks/test_status_updates.py tests/cli/test_entrypoint.py -q`;
  it passed with `27 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, workspace backup path helper cleanup:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Moved the backup destination path calculation from private
  `_backup_path_for_workspace(...)` into `WorkspaceBackupService.backup_path(...)`.
- Updated `WorkspaceBackupService.create(...)` to use the method and removed
  the old private workspace helper instead of keeping a shim.
- Verified the old helper name is absent with
  `rg -n "_backup_path_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused backup tests:
  `uv run pytest tests/state/test_backups.py -q`;
  it passed with `5 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task record-loading implementation helper cleanup:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Removed the remaining workspace-first record-loading implementation names
  `_load_task_runtime_for_workspace(...)` and
  `_load_tasks_from_store_for_workspace(...)`.
- Renamed their implementation bodies to local `_load_task_runtime_impl(...)`
  and `_load_tasks_from_store_impl(...)`, keeping
  `WorkspaceTasks.load_runtime(...)`, `WorkspaceTasks.list(...)`, and
  `WorkspaceTasks.list_state_first(...)` as the object method surface.
- Verified the old helper names are absent with
  `rg -n "_load_task_runtime_for_workspace|_load_tasks_from_store_for_workspace" litehive tests tests_integration docs/object-refactor-plan-2026-05-08.md docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused task persistence/runtime/recovery tests:
  `uv run pytest tests/state/test_task_persistence.py tests/state/test_task_runtime_storage.py tests/tasks/test_create_task.py tests/recovery/test_repair.py -q`;
  it passed with `50 passed, 2 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task creation implementation helper cleanup:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Removed the remaining workspace-first task creation implementation names
  `_highest_task_number_in_store_for_workspace(...)`,
  `_reserve_next_task_numbers_for_workspace(...)`,
  `_task_creation_stage_for_workspace(...)`,
  `_default_task_creation_source_for_workspace(...)`, and
  `_persist_created_tasks_for_workspace(...)`.
- Renamed their implementation bodies to local
  `_highest_task_number_in_store_impl(...)`,
  `_reserve_next_task_numbers_impl(...)`,
  `_task_creation_stage_impl(...)`,
  `_default_task_creation_source_impl(...)`, and
  `_persist_created_tasks_impl(...)`, keeping `WorkspaceTasks.create(...)`,
  `WorkspaceTasks.create_follow_ups(...)`, and `WorkspaceTasks.next_task_id(...)`
  as the object method surface.
- Updated the task-number characterization test to monkey-patch
  `_highest_task_number_in_store_impl(...)` so it still proves persisted task
  counters do not rescan the store when already populated.
- Verified the old helper names are absent with
  `rg -n "_highest_task_number_in_store_for_workspace|_reserve_next_task_numbers_for_workspace|_task_creation_stage_for_workspace|_default_task_creation_source_for_workspace|_persist_created_tasks_for_workspace" litehive tests tests_integration docs/object-refactor-plan-2026-05-08.md docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused creation/persistence/queue tests:
  `uv run pytest tests/tasks/test_create_task.py tests/state/test_task_persistence.py tests/state/test_task_runtime_storage.py tests/tasks/test_queue_invariants.py -q`;
  it passed with `59 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, task pipeline status private helper cleanup:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Removed the remaining workspace-first private status helper names
  `_runner_state_label_for_workspace(...)` and
  `_load_task_read_only_for_workspace(...)`.
- Renamed their implementation bodies to local `_runner_state_label_impl(...)`
  and `_load_task_read_only_impl(...)`, keeping
  `collect_task_pipeline_status_for_workspace(...)` as the existing imported
  status collection surface for CLI and daemon callers.
- Verified the old helper names are absent with
  `rg -n "_runner_state_label_for_workspace|_load_task_read_only_for_workspace" litehive tests tests_integration docs/object-refactor-plan-2026-05-08.md docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused status/health/daemon entry tests:
  `uv run pytest tests/observability/test_task_summary.py tests/cli/test_workspace_health.py tests/daemon/test_execution.py tests/cli/test_main_entrypoint.py -q`;
  it passed with `49 passed, 8 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, status snapshot probe method completion:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Completed the `StatusSnapshotCollector` probe method surface by adding
  `probe_last_cycle()`, `probe_heru_link()`, and
  `probe_task_index_references(...)`.
- Routed full snapshot collection through those methods instead of calling the
  probe helpers directly from `collect()`.
- Renamed the underlying workspace-first probe implementation names to local
  `_probe_runner_state_impl(...)`, `_probe_daemon_status_impl(...)`,
  `_probe_last_cycle_impl(...)`, `_probe_heru_link_impl(...)`,
  `_probe_origin_divergence_impl(...)`, and
  `_probe_task_index_references_impl(...)`.
- Updated the operational-status characterization test to monkey-patch
  `StatusSnapshotCollector` methods, preserving the exact check that
  doctor-style probes do not run on the operational path.
- Verified the old probe helper names are absent with
  `rg -n "_probe_runner_state_for_workspace|_probe_daemon_status_for_workspace|_probe_last_cycle_for_workspace|_probe_heru_link_for_workspace|_probe_origin_divergence_for_workspace|_probe_task_index_references_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused status diagnostics/summary/health tests:
  `uv run pytest tests/observability/test_status_diagnostics.py tests/observability/test_task_summary.py tests/cli/test_workspace_health.py -q`;
  it passed with `54 passed, 16 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, status snapshot loader method completion:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Added `StatusSnapshotCollector.load_runner()` and routed both full and
  operational snapshot collection through it.
- Renamed the remaining workspace-first status loader implementation names
  `_load_config_for_status_for_workspace(...)` and
  `_load_runner_status_for_status_for_workspace(...)` to local
  `_load_config_for_status_impl(...)` and
  `_load_runner_status_for_status_impl(...)`.
- Updated the direct runner-status serialization characterization test to
  import `_load_runner_status_for_status_impl(...)`.
- Updated `docs/proposed-object-structure-2026-05-08.md` so the
  `StatusSnapshotCollector` method list includes `load_runner()`,
  `probe_last_cycle()`, `probe_heru_link()`, and
  `probe_task_index_references()`.
- Verified the old loader helper names are absent with
  `rg -n "_load_config_for_status_for_workspace|_load_runner_status_for_status_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused status diagnostics/summary/health tests:
  `uv run pytest tests/observability/test_status_diagnostics.py tests/observability/test_task_summary.py tests/cli/test_workspace_health.py -q`;
  it passed with `54 passed, 14 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, lifecycle worktree setup object extraction:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Introduced `PipelineWorktreeSetup` in `litehive/lifecycle/worktree_setup.py`
  to bind one workspace to lifecycle worktree setup behavior.
- Moved the former private workspace-first helpers into methods:
  `resolve_worktree(...)`, `resolve_hook_execution_root(...)`,
  `task_recorded_worktree(...)`, `build_commit_node()`,
  `build_worktree_sync_node()`, `worktree_missing_probe()`,
  `worktree_metadata_repair()`, `mark_task_interrupted_on_crash(...)`,
  `cleanup_terminal_worktree(...)`, and
  `reconcile_terminal_commit_sha(...)`.
- Routed `run_task_for_workspace(...)` through one `PipelineWorktreeSetup`
  instance for hook root resolution, commit node construction, worktree sync
  construction, ready/recovery probes, crash cleanup, terminal commit-sha
  reconciliation, and terminal worktree cleanup.
- Updated tests that monkey-patched `build_commit_node_for_workspace(...)` to
  patch `PipelineWorktreeSetup.build_commit_node(...)`, and updated the direct
  terminal commit-sha reconciliation test to call
  `PipelineWorktreeSetup.reconcile_terminal_commit_sha(...)`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `PipelineWorktreeSetup` class shape.
- Verified the old helper names are absent with
  `rg -n "_resolve_worktree_for_workspace|_resolve_hook_execution_root_for_workspace|_task_recorded_worktree_for_workspace|_build_worktree_sync_node|_worktree_missing_probe|_worktree_metadata_repair|_mark_task_interrupted_on_crash|_cleanup_terminal_worktree|build_commit_node_for_workspace|reconcile_terminal_commit_sha_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused lifecycle/worktree tests:
  `uv run pytest tests/lifecycle/test_hooks_and_commit.py tests/lifecycle/test_launch_state_recovery.py tests/tasks/test_parked_lifecycle.py tests_integration/lifecycle/test_end_to_end.py -q`;
  it passed with `59 passed, 1 skipped, 2 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, worktree rescue private helper cleanup:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Removed the remaining private workspace-first rescue implementation names
  `_worktree_commits_ahead_of_main_for_workspace(...)`,
  `_worktree_patch_already_on_main_for_workspace(...)`,
  `_resolve_metadata_conflicts_for_workspace(...)`,
  `_drop_task_metadata_changes_for_workspace(...)`,
  `_finalize_rescue_for_workspace(...)`,
  `_ensure_unmerged_worktree_state_for_workspace(...)`,
  `_stash_litehive_changes_for_workspace(...)`,
  `_restore_litehive_changes_for_workspace(...)`, and
  `_worktree_has_non_metadata_changes_for_workspace(...)`.
- Renamed those local implementation helpers to `_..._impl(...)`, keeping
  `WorktreeRescueService.collect_rescue_candidates()`,
  `WorktreeRescueService.require_clean_main_checkout()`, and
  `WorktreeRescueService.apply_rescue_candidate(...)` as the object-facing
  rescue surface for callers.
- Verified the old helper names are absent with
  `rg -n "_worktree_commits_ahead_of_main_for_workspace|_worktree_patch_already_on_main_for_workspace|_resolve_metadata_conflicts_for_workspace|_drop_task_metadata_changes_for_workspace|_finalize_rescue_for_workspace|_ensure_unmerged_worktree_state_for_workspace|_stash_litehive_changes_for_workspace|_restore_litehive_changes_for_workspace|_worktree_has_non_metadata_changes_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused worktree rescue/support tests:
  `uv run pytest tests/cli/test_worktree_rescue.py tests/cli/test_worktree_support.py tests/cli/test_worktree_clean_with_active_runner.py tests/tasks/test_worktrees.py -q`;
  it passed with `16 passed, 10 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, daemon registry private helper cleanup:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Removed the remaining private workspace-first daemon lock wiring names
  `_daemon_lock_key_for_workspace(...)`, `_daemon_lock_path_for_workspace(...)`,
  `_daemon_lock_manager_for_workspace(...)`, and
  `_clear_stale_daemon_metadata_for_workspace(...)`.
- Renamed their implementation bodies to `_daemon_lock_key_impl(...)`,
  `_daemon_lock_path_impl(...)`, `_daemon_lock_manager_impl(...)`, and
  `_clear_stale_daemon_metadata_impl(...)`.
- Kept the public daemon registry API functions in place for this slice because
  CLI, daemon execution, termination, status, and tests still import them as
  the daemon registry boundary.
- Verified the old private helper names are absent with
  `rg -n "_daemon_lock_key_for_workspace|_daemon_lock_path_for_workspace|_daemon_lock_manager_for_workspace|_clear_stale_daemon_metadata_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused daemon registry/execution/process-state tests:
  `uv run pytest tests/daemon/test_registry.py tests/daemon/test_execution.py tests/state/test_process_state.py tests/cli/test_workspace_health.py -q`;
  it passed with `28 passed, 6 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, runner lock private helper cleanup:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Removed the remaining private workspace-first runner lock implementation
  names `_runner_lock_key_for_workspace(...)` and
  `_runner_lock_manager_for_workspace(...)`.
- Renamed their implementation bodies to `_runner_lock_key_impl(...)` and
  `_runner_lock_manager_impl(...)`, keeping `WorkspaceRunnerLock` as the
  object-facing runner lock API and leaving `workspace_lock_for_workspace(...)`
  untouched as the broader short critical-section lock used across state
  mutations.
- Verified the old private helper names are absent with
  `rg -n "_runner_lock_key_for_workspace|_runner_lock_manager_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused process/runner/recovery/status tests:
  `uv run pytest tests/state/test_process_state.py tests/recovery/test_runner_recovery.py tests/lifecycle/test_launch_state_recovery.py tests/tasks/test_runtime_updates.py tests/tasks/test_status_updates.py -q`;
  it passed with `37 passed`.
- Re-ran `make typecheck`; it passed with `0 errors`.

Continuation verification 2026-05-08, daemon registry object migration:
- Rechecked the completed plan for unchecked items with
  `rg -n "\\[ \\]" docs/object-refactor-plan-2026-05-08.md`; it still has no
  matches.
- Introduced `DaemonRegistry` in `litehive/daemon/registry.py` as the
  workspace-bound daemon registration API.
- Moved the public daemon registry operations into methods:
  `lock_is_active()`, `metadata()`, `live_entry()`, `register(...)`,
  `unregister(...)`, `touch(...)`, and `stale_metadata()`.
- Removed the old module-level registry functions instead of leaving shims:
  `daemon_lock_is_active_for_workspace(...)`,
  `daemon_metadata_for_workspace(...)`,
  `get_workspace_daemon_for_workspace(...)`,
  `register_daemon_for_workspace(...)`,
  `unregister_daemon_for_workspace(...)`,
  `touch_daemon_for_workspace(...)`, and
  `stale_daemon_metadata_for_workspace(...)`.
- Removed the unused `daemon_registry_for_workspace(...)` factory after
  migrating callers to direct `DaemonRegistry(workspace)` construction.
- Migrated daemon execution, daemon termination, daemon status probes, backup
  restore checks, workspace health, daemon registry tests, process-state tests,
  backup tests, and workspace-health tests to `DaemonRegistry`.
- Updated `docs/proposed-object-structure-2026-05-08.md` with the
  `DaemonRegistry` class shape.
- Verified the old registry function names are absent with
  `rg -n "daemon_registry_for_workspace|daemon_lock_is_active_for_workspace|daemon_metadata_for_workspace|get_workspace_daemon_for_workspace|register_daemon_for_workspace|unregister_daemon_for_workspace|touch_daemon_for_workspace|stale_daemon_metadata_for_workspace" litehive tests tests_integration docs/proposed-object-structure-2026-05-08.md`;
  it returned no matches.
- Re-ran focused daemon registry/execution/process-state/backup/health tests:
  `uv run pytest tests/daemon/test_registry.py tests/daemon/test_execution.py tests/state/test_process_state.py tests/state/test_backups.py tests/cli/test_workspace_health.py -q`;
  it passed with `33 passed, 10 warnings`.
- Re-ran `make typecheck`; it passed with `0 errors`.
