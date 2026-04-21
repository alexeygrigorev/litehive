I’m inspecting the runner, runtime persistence, and status/reporting paths first so I can wire parallel worker fanout without breaking the existing lifecycle semantics. After that I’ll patch the implementation and add focused lifecycle tests around a real single-task run.
I found existing fanout-oriented tests at the end of `tests/test_workspace.py`, which suggests part of the behavior may already be scaffolded but not fully wired through runtime persistence and reporting. I’m reading the runtime models and executor code now to close that gap instead of layering a second mechanism on top.
The current code already has task-level worker state in `runtime.workers`, but the fast-path status and interruption handling still look biased toward a single active/last subagent. I’m tracing `build_executor` and the task save helpers next so I can make fanout use those existing runtime primitives consistently.
The executor already fans out by plan items, and there are tests for summary rendering, so the remaining work is likely around the runtime transition edges: how we persist multiple running workers, how interruption handoff chooses context, and what the fast status path exposes. I’m reading the summary renderer and the task-run start/finish transitions before editing.
I have enough context to patch safely. Next I’m running the fanout-related tests as they exist now to see which acceptance gaps are still real, then I’ll edit the runtime/observability code around those concrete failures.
I’ve confirmed the currently committed tests only cover part of the acceptance surface. I’m tightening the implementation around two likely gaps: per-worker artifact metadata during live fanout, and status output that still compresses parallel work down to a count in the fast path.
I’m editing three areas now: worker artifact metadata, worker visibility in status output, and one real lifecycle test that exercises fanout through `run_next_task` rather than only `build_executor` directly.
The patch is in place. I’m running the focused fanout suite now; if anything fails it will likely be in the new live-artifact metadata test or the lifecycle ordering check, since those hit edges the existing tests weren’t exercising.
The focused fanout suite is green. I’m running a slightly wider slice now that includes the existing status, interruption, and subagent persistence tests around the same code paths to catch regressions before I close out.
VERDICT: PASS
SUMMARY: Added parallel-worker visibility and live artifact metadata so implementing fanout persists and reports multiple workers cleanly through execution and recovery.
FILES_CHANGED:
- litehive/cli.py
- litehive/main.py
- litehive/observability.py
- litehive/subagents.py
- litehive/tasks.py
- tests/test_workspace.py
TESTS_ADDED: 3
TESTS_PASSING: 19
WARNINGS:
- Verification was focused on targeted fanout/status/recovery tests, not the full test suite.
- `uv` reported a `VIRTUAL_ENV` mismatch warning during pytest runs, but the targeted tests still passed.

[stderr]
Reading additional input from stdin...
2026-04-03T06:17:49.634640Z ERROR codex_core::tools::router: error=apply_patch verification failed: Failed to find expected lines in /home/alexey/git/litehive/.litehive/worktrees/T-0106-add-parallel-worker-fanout-within-a-single-task/litehive/subagents.py:
            task.subagents.append(ref)
            save_task(self.root, task)
            mark_subagent_started(self.root, task, ref, step=step)
        self._write_session_start(base, ref, prompt)
