# T-0279 Isolate workspace execution from inherited operator VIRTUAL_ENV

- Mode: tasks
- Task type: bugfix
- PM complexity: -
- Planned effort: -

## Goal
Prevent child workspace runs from inheriting the operator shell's active Python virtual environment.

## Acceptance Criteria
- Workspace-scoped commands sanitize inherited VIRTUAL_ENV when targeting another repo.
- Subagents and helper commands prefer the target workspace environment instead of the caller's active venv.
- Logs no longer show uv warnings about VIRTUAL_ENV from the parent repo leaking into child workspace execution.

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
