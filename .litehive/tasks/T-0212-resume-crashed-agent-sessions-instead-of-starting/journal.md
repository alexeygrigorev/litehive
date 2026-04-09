# T-0212 Resume crashed agent sessions instead of starting from scratch

## 2026-04-07T15:41:35+00:00
Task created.

## 2026-04-07T16:26:12+00:00
Created task worktree at `.litehive/worktrees/T-0212-resume-crashed-agent-sessions-instead-of-starting`.

## 2026-04-07T16:26:12+00:00
Execution started with engine `claude`.

## 2026-04-07T16:33:25+00:00
[guard] Rejected empty SWE pass: SWE reported pass but produced no file changes and no tests. This usually means the agent did not actually write code. Worktree: unknown. Report summary: ## Implementation Summary

## 2026-04-07T16:37:59+00:00
[guard] Rejected empty SWE pass: SWE reported pass but produced no file changes and no tests. This usually means the agent did not actually write code. Worktree: unknown. Report summary: ## Implementation Summary

## 2026-04-07T16:38:38+00:00
[guard] Rejected empty SWE pass: SWE reported pass but produced no file changes and no tests. This usually means the agent did not actually write code. Worktree: unknown. Report summary: ## Implementation Complete

## 2026-04-07T16:39:14+00:00
[guard] Rejected empty SWE pass: SWE reported pass but produced no file changes and no tests. This usually means the agent did not actually write code. Worktree: unknown. Report summary: ## Implementation Complete

## 2026-04-07T16:39:14+00:00
Execution finished with status `flagged`.

## 2026-04-07T20:53:10+00:00
Task requeued for another implementation pass.

## 2026-04-08T06:19:04+00:00
[worktree] Rebase onto 3bfe1373 failed. Launching merge agent.

## 2026-04-08T06:19:04+00:00
[worktree] Merge conflict on 2 file(s). Launching merge agent.

## 2026-04-08T06:22:05+00:00
[worktree] Merge agent resolved conflicts.

## 2026-04-08T06:22:05+00:00
Execution started with engine `claude`.

## 2026-04-08T06:22:55+00:00
[guard] Rejected empty SWE pass: SWE reported pass but produced no file changes and no tests. This usually means the agent did not actually write code. Worktree: unknown. Report summary: ## Implementation Complete

## 2026-04-08T06:23:53+00:00
[guard] Rejected empty SWE pass: SWE reported pass but produced no file changes and no tests. This usually means the agent did not actually write code. Worktree: unknown. Report summary: ## Implementation Complete

## 2026-04-08T06:24:43+00:00
[guard] Rejected empty SWE pass: SWE reported pass but produced no file changes and no tests. This usually means the agent did not actually write code. Worktree: unknown. Report summary: ## Implementation Complete

## 2026-04-08T06:25:25+00:00
[guard] Rejected empty SWE pass: SWE reported pass but produced no file changes and no tests. This usually means the agent did not actually write code. Worktree: unknown. Report summary: ## Implementation Complete

## 2026-04-08T06:25:26+00:00
Execution finished with status `flagged`.

## 2026-04-08T19:02:54+00:00
Task requeued for another implementation pass.

## 2026-04-09T00:31:56+00:00
Created task worktree at `.litehive/worktrees/T-0212-resume-crashed-agent-sessions-instead-of-starting`.

## 2026-04-09T00:31:56+00:00
Execution started with engine `codex`.

## 2026-04-09T00:32:48+00:00
[guard] Rejected empty SWE pass: SWE reported pass but produced no file changes and no tests. This usually means the agent did not actually write code. Worktree: unknown. Report summary: Verified the current branch already implements crash-resume for unclassified Claude agent exits in litehive/runtime/_builder.py. Evidence: the executor loop tracks a single crash_resume_attempted flag, detects failure is None with non-zero exit, extracts the continuation via extract_engine_continuation, sets resume_session_id, appends a journal event, and retries with the existing resume prompt 'Please continue where you left off. Complete the task.' Only Claude sessions are resumed, and only once per crash; a second crash falls through normally. I ran uv run pytest tests/test_crash_resume.py -q and confirmed 4/4 passing in 2.79s, covering: Claude crash resumes same session ID, resumed prompt contains 'continue where you left off', only one resume attempt occurs, non-Claude engines skip resume, and the journal records the resume attempt clearly. git status is clean and there were no additional code changes needed in this worktree.

## 2026-04-09T00:33:22+00:00
[guard] Rejected empty SWE pass: SWE reported pass but produced no file changes and no tests. This usually means the agent did not actually write code. Worktree: unknown. Report summary: Verified the existing implementation in litehive/runtime/_builder.py satisfies T-0212 without further code changes. The executor loop now tracks crash_resume_attempted, detects an unclassified non-zero exit (failure is None and exit_code != 0), extracts the continuation via extract_engine_continuation, and for claude only retries once with resume_session_id. The resumed invocation uses the prompt 'Please continue where you left off. Complete the task.' and appends a clear journal event noting the crash, exit code, and resumed session id. If the resumed claude session crashes again, the single-attempt guard prevents another resume and execution proceeds through the normal failure path. Verified with uv run pytest tests/test_crash_resume.py -q: 4 passed in 1.73s. Those tests cover: claude crash resumes the same session id, the resumed prompt contains 'continue where you left off', only one resume attempt occurs, non-claude engines do not resume, and the journal records the resume attempt clearly. git status is clean.

## 2026-04-09T00:33:59+00:00
[guard] Rejected empty SWE pass: SWE reported pass but produced no file changes and no tests. This usually means the agent did not actually write code. Worktree: unknown. Report summary: Verified the existing implementation in litehive/runtime/_builder.py satisfies T-0212 without further code changes. The executor loop tracks crash_resume_attempted, detects unclassified non-zero exits (failure is None and exit_code != 0), and for claude only extracts the continuation via extract_engine_continuation, sets resume_session_id, appends a clear journal event, and retries once with the existing resume prompt 'Please continue where you left off. Complete the task.' If the resumed claude session crashes again, the single-attempt guard prevents another resume and execution falls through the normal failure path. Non-claude engines skip this path. Verified with: uv run pytest tests/test_crash_resume.py -q -> 4 passed in 1.50s. The tests cover: claude crash resumes the same session id, resumed prompt contains 'continue where you left off', only one resume attempt occurs, non-claude engines do not resume, and the journal records the resume attempt clearly. git status was clean at verification time.

## 2026-04-09T00:34:47+00:00
[guard] Rejected empty SWE pass: SWE reported pass but produced no file changes and no tests. This usually means the agent did not actually write code. Worktree: unknown. Report summary: Verified the existing implementation in litehive/runtime/_builder.py satisfies T-0212 without further code changes. The executor loop tracks crash_resume_attempted, detects unclassified non-zero exits (failure is None and exit_code != 0), and for claude only extracts the continuation via extract_engine_continuation, sets resume_session_id, appends a clear journal event, and retries once with the existing resume prompt 'Please continue where you left off. Complete the task.' If the resumed claude session crashes again, the single-attempt guard prevents another resume and execution falls through the normal failure path. Non-claude engines skip this path. Verified with: uv run pytest tests/test_crash_resume.py -q -> 4 passed in 1.83s. The tests cover: claude crash resumes the same session id, resumed prompt contains 'continue where you left off', only one resume attempt occurs, non-claude engines do not resume, and the journal records the resume attempt clearly. git status was clean at verification time.

## 2026-04-09T00:34:47+00:00
Execution finished with status `flagged`.
