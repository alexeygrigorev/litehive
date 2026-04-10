# T-0280 Handle no-op merges in commit_to_git without leaving tasks stuck in merge_failed

- Mode: tasks
- Task type: bugfix
- PM complexity: simple
- Planned effort: s

## Goal
Make commit_to_git treat already-landed/no-op integrations as successful reconciliation instead of routing the task into merge_failed.

## Acceptance Criteria
- When the task worktree's intended patch is already present on main and final integration produces no new commit, commit_to_git finishes the task in a non-stuck success state instead of merge_failed.
- The task record and operator-visible report/journal explain that the work was already landed or the merge was a no-op, and record the reconciled main commit SHA used to finish the task.
- Real merge conflicts still surface as merge failures, and operator-facing output distinguishes conflict failures from already-landed/no-op reconciliation.

## Constraints
- Prefer the smallest change in commit_to_git/recovery paths that removes the stuck merge_failed outcome without broad workflow refactoring.
- Call out any remaining edge cases where Git reports success but main/worktree divergence still needs explicit follow-up.

## Plan
- Add a focused regression that reproduces a task worktree whose change is already present on main and currently risks being treated as a failed/no-op integration.
- Update commit_to_git reconciliation logic to detect already-landed/no-op integration separately from real merge conflicts, then mark the task done with a clear reason and commit SHA.
- Keep conflict behavior intact and add/adjust operator-facing summary or journal text so reviewers can tell no-op reconciliation from merge_conflict failures.
- Run the affected pytest slice around commit_to_git, recovery, and merge-agent behavior to prove the new path and guard existing conflict handling.

## PM Sizing
- Complexity: simple
- Planned effort: s

## Template Guidance
- Describe the broken behavior, trigger, and expected correct behavior before changing code.
- Aim at root cause, not just the visible symptom.
- Include regression coverage or equivalent focused proof that the failure is gone.

## Intake Notes

### Bug and Reproduction
- Describe the failing behavior, trigger, and expected result.

_TBD_

### Root Cause
- Note the suspected or confirmed cause in the affected path.

_TBD_

### Regression Coverage
- Record the exact test or check that prevents recurrence.

_TBD_
