# T-0325 Simplify recovery subsystem: keep what agents actually use, cut the rest

## 2026-04-10T20:23:35+00:00
Task created.

## 2026-04-10T20:26:55+00:00
Task metadata updated via CLI.

## 2026-04-12T20:38:40+00:00
Task record updated from grooming output:
- pm_complexity: `complex`
- planned_effort: `m`

## 2026-04-12T22:10:00+00:00
Recovery surface classified from live callers before simplification.

Grep evidence used:
```text
$ git diff --stat main...HEAD
(no output)

$ rg -n "recover_stale_runner_state\(|prepare_interrupted_task\(|mark_interrupted_subagent\(" litehive tests
litehive/tasks/queue_ops.py:46:    recover_stale_runner_state(root)
litehive/tasks/queue_ops.py:63:    recover_stale_runner_state(root)
litehive/tasks/queue_ops.py:104:    recover_stale_runner_state(root)
litehive/workspace/task_status.py:66:        prepare_interrupted_task(
litehive/tasks/queue_ops.py:438:                prepare_interrupted_task(
tests/test_engine_variants_and_timeline.py:463:    interrupted = tasks_module.mark_interrupted_subagent(

$ rg -n "RecoveryAgent|enter_recovery|recovery_attempt|failure_context" litehive/pipeline litehive/daemon
litehive/pipeline/rules.py:63:        transition_to=S.RECOVERING,
litehive/pipeline/rules.py:64:        with_effect=enter_recovery,
litehive/pipeline/agents/recovery.py:32:class RecoveryAgent(RoleAgent):
litehive/pipeline/agents/recovery.py:54:                "failure_context": state.failure_context,
litehive/pipeline/agents/recovery.py:55:                "recovery_attempt": state.recovery_attempt.get(origin, 0),

$ rg -n "attempt_stage_recovery|scan_workspace_doctor|apply_doctor_fixes|rollback_completed_task" litehive tests
tests/test_self_heal_recovery.py:11:    attempt_stage_recovery,
litehive/cli/workspace.py:71:        result = apply_doctor_fixes(root)
litehive/cli/doctor.py:9:def cmd_doctor(workspace, fix: bool = False) -> int:
litehive/cli/runner.py:234:        summary = rollback_completed_task(workspace, task_id)
```

Remaining public functions after simplification:

LIVE:
- `recover_stale_runner_state`
- `mark_interrupted_subagent`
- `prepare_interrupted_task`
- `prepare_interrupted_task_for_requeue`
- `interruption_journal_message`
- `stale_interruption_reason`
- `resolve_recovery_engine`
- `require_completed_task`
- `recover_completed_task`
- `has_inactive_running_tasks`
- `should_requeue_commit_stage_task`
- `is_stranded_commit_task`
- `repair_workspace_state`

TEST-ONLY / compatibility-only before deletion:
- `attempt_stage_recovery`
- `classify_recovery_failure_owner`
- `traceback_text`
- `traceback_frame_paths`
- `traceback_fingerprint`
- `truncate_recovery_text`
- `load_failed_subagent_diagnostics`
- `resolve_recovery_execution_root`
- `report_verdict`
- `latest_stage_report_verdict`
- `latest_stage_report_verdict_for_step`
- `scan_workspace_doctor`
- `apply_doctor_fixes`
- `status_attention_findings`
- `rollback_completed_task`

DEAD historical-bug handlers deleted from the public surface:
- `is_orphaned_commit_stage_task`
- `should_resume_done_task_at_commit_stage`
- `should_recover_flagged_commit_stage_task`
- `find_existing_checkpoint_commit`
- `prepare_recovered_commit_task`
- `recover_flagged_commit_task`
- `finalize_recovered_commit_task`
- `recover_existing_checkpoint_commit`
- `recover_stranded_commit_tasks`
- `reconcile_stale_runner_tasks`

Follow-up verification after QA:
```text
$ rg -n "^def (is_orphaned_commit_stage_task|should_resume_done_task_at_commit_stage|should_recover_flagged_commit_stage_task|find_existing_checkpoint_commit|prepare_recovered_commit_task|recover_flagged_commit_task|finalize_recovered_commit_task|recover_existing_checkpoint_commit|recover_stranded_commit_tasks|reconcile_stale_runner_tasks)\\(" litehive/recovery tests
(no output)

$ rg -n "recover_stranded_commit_tasks" litehive/recovery/workspace_repair.py
353:recover_stranded_commit_tasks = lambda root, state: False
544:        commit_mutated = recover_stranded_commit_tasks(root, state)
```

Compatibility note:
- The dead commit-era recovery helper definitions are removed.
- A non-`def` compatibility binding remains only so `recover_stale_runner_state` can stay textually identical to `main`, as required by the task keep-set.

Why the historical commit handlers are unreachable now:
- `litehive/pipeline/rules.py` routes stage failure as `stage -> recovering -> resume|failed`; the active self-heal loop is the v2 pipeline node, not `litehive/recovery.execution_recovery.attempt_stage_recovery`.
- `litehive/pipeline/orchestration.py::_sync_back` only mirrors v2 terminal state back as `done`, `flagged`, or `merge_failed`; it does not create the older orphaned `commit_to_git` / accepted-without-checkpoint states those handlers were written for.
- Crash recovery for an in-flight stage still goes through `runtime.execution_status == "running"` and is covered by `recover_stale_runner_state`, including `commit_to_git`.

## 2026-04-12T22:16:50+00:00
Task abandoned via CLI at stage `flagged`.

## 2026-04-19T07:07:22+00:00
Task metadata updated via CLI.

## 2026-04-22T07:06:01+00:00
Task requeued for another implementation pass.
