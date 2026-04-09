# T-0277 Fix config loading regression for legacy claude_enabled workspaces

- Mode: tasks
- Task type: bugfix
- PM complexity: moderate
- Planned effort: m

## Goal
Litehive CLI commands should load older workspace configs that still contain claude_enabled instead of crashing with a LitehiveConfig TypeError.

## Acceptance Criteria
- litehive status/queue/add work on workspaces whose .litehive/config.yaml still contains claude_enabled
- Config loading ignores, migrates, or normalizes deprecated claude_enabled safely
- A regression test covers loading a legacy config with claude_enabled

## Constraints
- Prefer the smallest change that removes the failure mode.
- Call out any remaining edge cases or follow-up risk explicitly.

## Plan
- Reproduce or localize the failing behavior.
- Implement the minimal targeted fix.
- Run focused regression coverage for the affected behavior.

## PM Sizing
- Complexity: moderate
- Planned effort: m

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
