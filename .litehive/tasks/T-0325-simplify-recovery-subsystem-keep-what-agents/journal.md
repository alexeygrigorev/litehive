# T-0325 Simplify recovery subsystem: keep what agents actually use, cut the rest

## 2026-04-24T12:00:00+02:00
Recovery surface classification

Defined public functions in the current source-of-truth package:

- `litehive/recovery/detection.py:67:def best_effort_recovery_task`
- `litehive/recovery/detection.py:99:def corrupt_task_launch_diagnostics`
- `litehive/recovery/detection.py:115:def detect_cycle_start_failure`
- `litehive/recovery/execution_recovery.py:91:def prepare_task_launch`
- `litehive/recovery/execution_recovery.py:97:def attempt_launch_recovery`
- `litehive/recovery/execution_recovery.py:150:def flag_task_after_failed_launch_recovery`
- `litehive/recovery/execution_recovery.py:206:def mark_interrupted_subagent`
- `litehive/recovery/execution_recovery.py:237:def prepare_interrupted_task`
- `litehive/recovery/execution_recovery.py:267:def interruption_journal_message`
- `litehive/recovery/execution_recovery.py:291:def stale_interruption_reason`
- `litehive/recovery/execution_recovery.py:302:def recover_stale_runner_state`
- `litehive/recovery/workspace_repair.py:10:def repair_workspace_state`

Classification with grep evidence:

- LIVE `best_effort_recovery_task`
  grep: `litehive/daemon/execution.py:346,475,549`; `litehive/cli/runner.py:152`
- LIVE `corrupt_task_launch_diagnostics`
  grep: `litehive/recovery/execution_recovery.py:481`; `litehive/cli/runner.py:159`
- LIVE `detect_cycle_start_failure`
  grep: `litehive/daemon/execution.py:366,472`
- LIVE `prepare_task_launch`
  grep: `litehive/cli/runner.py:181`
- LIVE `attempt_launch_recovery`
  grep: `litehive/cli/runner.py:238`; `litehive/daemon/execution.py:349`
- LIVE `flag_task_after_failed_launch_recovery`
  grep: `litehive/cli/runner.py:241`; `litehive/daemon/execution.py:353,477,551`
- LIVE `mark_interrupted_subagent`
  grep: `litehive/recovery/execution_recovery.py:805` from `_set_interruption_metadata()` on the ctrl-c / stale-runner path
- LIVE `prepare_interrupted_task`
  grep: `litehive/tasks/queue.py:747,778`; `litehive/recovery/execution_recovery.py:925`
- LIVE `interruption_journal_message`
  grep: `litehive/tasks/queue.py:761,793`; `litehive/recovery/execution_recovery.py:941`
- LIVE `stale_interruption_reason`
  grep: `litehive/tasks/queue.py:752,783`; `litehive/recovery/execution_recovery.py:930`
- LIVE `recover_stale_runner_state`
  grep: `litehive/tasks/queue.py:361,380,415`; `litehive/cli/queue_cli.py:39`; `litehive/tasks/status.py:243`; `litehive/recovery/workspace_repair.py:13`
- LIVE `repair_workspace_state`
  grep: `litehive/cli/workspace.py:150`; `litehive/state/locking.py:276`; `litehive/recovery/execution_recovery.py:575`

Deleted as DEAD after grep showed no live callers in the current tree:

- DEAD `analyze_scope_changes` (`main:litehive/recovery/scope_analysis.py:143`)
- DEAD `attribute_test_failure` (`main:litehive/recovery/test_failure_attribution.py:47`)
- DEAD `build_unrelated_test_follow_up` (`main:litehive/recovery/test_failure_attribution.py:104`)
- DEAD launch compatibility shim `litehive/recovery/launch.py`

Supporting grep outcomes:

- `rg -n "analyze_scope_changes|attribute_test_failure|build_unrelated_test_follow_up" litehive tests`
  output: `no live callers in current tree`
- `rg -n "litehive\\.recovery\\.launch|from litehive\\.recovery\\.launch" litehive tests`
  output: `launch shim has no callers in current tree`

Historical-bug handler verification

The prompt named `_is_orphaned_commit_stage_task`, `_should_resume_done_task_at_commit_stage`, `_recover_existing_checkpoint_commit`, and `_recover_flagged_commit_task`.

- `sh -lc 'if rg -n "_is_orphaned_commit_stage_task|_should_resume_done_task_at_commit_stage|_recover_existing_checkpoint_commit|_recover_flagged_commit_task" litehive tests; then :; else echo "no matches in current tree"; fi'`
  output: `no matches in current tree`
- `sh -lc 'if git grep -n -e "_is_orphaned_commit_stage_task" -e "_should_resume_done_task_at_commit_stage" -e "_recover_existing_checkpoint_commit" -e "_recover_flagged_commit_task" main -- "*.py"; then :; else echo "no matches in main"; fi'`
  output: `no matches in main`

Why the old reproducing shapes are unreachable now:

- `litehive/lifecycle/orchestration.py:276-277` and `litehive/worktree.py:876-877` write successful `commit_to_git` completions to `status="done"` and `pipeline_status="done"` instead of leaving tasks stranded at `commit_to_git`.
- `litehive/tasks/runtime.py:131-147` clears `active_task_id` and removes terminal tasks from the queue in `finish_task_run_transition()`.
- `litehive/tasks/queue.py:746-788` plus `litehive/recovery/execution_recovery.py:682,925-937,1003-1158` now handle stale `commit_to_git` and other resumable stages through the generic requeue path (`prepare_interrupted_task` + queue normalization), so there is no separate commit-stage-only repair lane left to maintain.

Conclusion: the historical one-off commit-stage handlers named in the prompt are absent from both the current tree and `main`, and their old incident shapes are covered by the generic stale-runner / resumable-task path that remains in use.
