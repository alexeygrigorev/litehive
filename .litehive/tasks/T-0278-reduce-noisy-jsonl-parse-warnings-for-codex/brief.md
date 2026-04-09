# T-0278 Reduce noisy JSONL parse warnings for Codex command_execution payloads

- Mode: tasks
- Task type: bugfix
- PM complexity: moderate
- Planned effort: m

## Goal
Litehive should not spam iter_jsonl_payloads warnings when Codex emits command_execution payloads whose embedded command/output strings contain unescaped shell content.

## Acceptance Criteria
- Codex run logs with command_execution items no longer flood the logs with repeated skipping unparseable line warnings
- Parser behavior remains safe and does not misclassify valid engine events
- A focused regression test covers the observed command_execution payload shape or equivalent malformed-line case

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
