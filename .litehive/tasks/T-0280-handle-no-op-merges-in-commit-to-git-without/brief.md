# T-0280 Handle no-op merges in commit_to_git without leaving tasks stuck in merge_failed

- Mode: tasks
- Task type: bugfix
- PM complexity: -
- Planned effort: -

## Goal
Make commit_to_git reconcile tasks correctly when the intended changes are already present on the target branch and merge produces no new commit.

## Acceptance Criteria
- If merge produces no new commit because the patch is already present on main, Litehive does not leave the source task in merge_failed.
- Task state is reconciled to a non-stuck outcome with a clear reason when merge work is already applied.
- Operator-facing logs distinguish real merge conflicts from already-landed/no-op merges.

## Constraints
- Prefer the smallest change that removes the failure mode.
- Call out any remaining edge cases or follow-up risk explicitly.

## Plan
- Reproduce or localize the failing behavior.
- Implement the minimal targeted fix.
- Run focused regression coverage for the affected behavior.

## PM Sizing
- Complexity: Not estimated.
- Planned effort: Not sized.

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
