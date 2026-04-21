I’m inspecting the current runner, runtime, and status/subagent plumbing to see how single-worker execution is represented today, then I’ll patch in deterministic parallel fanout and verify it through the real CLI/task lifecycle paths.
I’ve confirmed the current runtime model is still centered on `active_subagent`/`last_subagent`. Next I’m reading the task runtime models, subagent manager, and status rendering paths to replace that with a multi-worker view without breaking existing stage flow.
I’ve narrowed the execution seam: all stage work still funnels through a single `SubagentManager.run()` call inside `build_executor()`. I’m now checking whether the repo already has any deterministic planning knobs I can reuse for worker count and worker identity before I patch the executor and runtime schema.
The repo doesn’t already have a worker-fanout primitive. I’m using the task’s persisted `plan` as the deterministic source for worker assignments in `implementing`, then I’ll update interruption/recovery helpers that currently assume exactly one active subagent.
I have the design pinned down. I’m patching three areas together now so the behavior stays coherent: runtime schema/persistence for multiple workers, parallel execution in the `implementing` stage, and status rendering/recovery paths that currently assume exactly one active subagent.
I’m editing the runtime and task persistence layer first so the executor can safely write multi-worker state. After that I’ll wire the actual `implementing` fanout and add targeted tests around the CLI-visible behavior.
The code paths are patched. I’m running targeted tests now to catch schema/runtime issues before I add or adjust the fanout-specific assertions.
I found a compatibility edge while wiring fanout: many existing tests monkeypatch `SubagentManager.run()` with the old signature. I’m narrowing the new call shape so single-worker stages still use the old call contract, and only fanout paths pass worker metadata.
The new tests are in. I’m running the fanout/recovery/status slice now, then I’ll fix any behavioral regressions from the executor refactor before deciding whether a broader pass is necessary.
The runner entrypoint is re-normalizing the task before it hits the patched fanout path, so that specific test isn’t isolating the behavior I need. I’m tightening it to the stage executor itself, which is the actual fanout boundary, while keeping the separate stale-recovery and CLI-status tests on the real persisted lifecycle paths.
I hit a backward-compatibility bug in stale-runner recovery: older runtime records only populate `active_subagent`, not the new `workers` list. I’m patching the recovery helper to treat legacy single-worker state as a running worker so existing flows stay intact.
The fanout slice is stable under targeted checks. I’m running the full `tests/test_workspace.py` file once now because the executor refactor touches common stage execution paths, and I want to catch any old monkeypatch/signature assumptions before I stop.
The full file surfaced wider regressions and then appeared to stall, which is consistent with one of the new execution paths interacting badly with existing tests. I’m stopping that sweep and switching to `--maxfail=1` so I can fix the first real compatibility failure instead of chasing aggregate dots.
I’m drilling into the non-fanout regressions now. Two broad issues showed up: I made the mutation guard too permissive across threads, and some single-worker stage bookkeeping is now leaking fanout semantics into normal runs. I’m reproducing one of the runner regressions directly so I can fix the actual branch instead of papering over test expectations.
I’m cutting two compatibility fixes now that are clearly from this change set: single-worker runs should keep the old journal/report wording, and only actual fanout worker threads should bypass the runner-thread mutation guard. That should knock out most of the regressions that are genuinely caused by the new fanout plumbing.
VERDICT: FAIL
SUMMARY: Added implementing-stage worker fanout, persisted multi-worker runtime state, and multi-worker status rendering, but the full workspace test suite still has regressions to resolve.
FILES_CHANGED:
- litehive/cli.py
- litehive/main.py
- litehive/models.py
- litehive/observability.py
- litehive/runtime.py
- litehive/subagents.py
- litehive/tasks.py
- tests/test_workspace.py
TESTS_ADDED: 4
TESTS_PASSING: 7
WARNINGS:
- `uv run pytest -q tests/test_workspace.py -k 'subagent_manager_persists_planner_and_reviewer_artifacts or render_task_summary_includes_active_subagent_pid or render_task_summary_and_status_show_parallel_workers or recover_stale_runner_state_recovers_running_task_without_runner_lock_record or recover_stale_runner_state_marks_parallel_workers_interrupted or run_next_task_creates_checkpoint_commit_and_persists_policy or run_next_task_executes_stage_in_task_worktree'` passed
- `uv run pytest -q tests/test_workspace.py -k 'parallel_workers or multiple_runtime_workers or stale_runner_state_marks_parallel_workers'` passed
- `uv run pytest -q tests/test_workspace.py` failed with 33 regressions, including queue/runner lifecycle expectations and several single-worker retry/fallback expectation mismatches

[stderr]
Reading additional input from stdin...
2026-04-03T05:48:40.113717Z ERROR codex_core::tools::router: error=apply_patch verification failed: Failed to find expected lines in /home/alexey/git/litehive/.litehive/worktrees/T-0106-add-parallel-worker-fanout-within-a-single-task/litehive/subagents.py:
    def _record_subagent_pid(self, task: TaskRecord, base: Path, ref: SubagentRef, pid: int | None) -> None:
        if pid is None:
            return
        mark_subagent_pid(self.root, task, pid)
        self._write_session_metadata(
            base,
            ref,
2026-04-03T05:52:57.374084Z ERROR codex_core::tools::router: error=apply_patch verification failed: Failed to find expected lines in /home/alexey/git/litehive/.litehive/worktrees/T-0106-add-parallel-worker-fanout-within-a-single-task/tests/test_workspace.py:
    assert any("subagent=SA-0001 swe/codex running pid=4242" in line for line in lines)
2026-04-03T05:54:08.208784Z ERROR codex_core::tools::router: error=apply_patch verification failed: Failed to find expected lines in /home/alexey/git/litehive/.litehive/worktrees/T-0106-add-parallel-worker-fanout-within-a-single-task/tests/test_workspace.py:
def test_recover_stale_runner_state_when_subagent_is_active(tmp_path: Path) -> None:
2026-04-03T06:00:30.812693Z ERROR codex_core::tools::router: error=write_stdin failed: stdin is closed for this session; rerun exec_command with tty=true to keep stdin open
