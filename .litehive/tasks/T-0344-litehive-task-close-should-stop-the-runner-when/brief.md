# T-0344 'litehive task close' should stop the runner when closing an actively-running task

- Mode: tasks
- Task type: bugfix
- PM complexity: moderate
- Planned effort: s

## Goal
Today, closing an actively-running task is rejected by the concurrency guard and the user has to stop the daemon manually before retrying close. This is the wrong default: if a user explicitly asks to close a task, they are telling litehive to abandon the work, and litehive should honor that by stopping the runner as part of the close. Otherwise 'close' is only usable on idle tasks, which defeats its purpose for the common 'this task is going the wrong direction, kill it' workflow.

## Acceptance Criteria
- Running 'litehive task close <id>' on the currently-active task succeeds without requiring a prior 'litehive stop'
- Any in-flight subagent/engine execution for the closed task is actually stopped, not left running in the background
- The task ends up in the same final state as closing an idle task (status set to the outcome, removed from queue, active_task_id cleared, journal entry written)
- tests/ covers the 'close an active task' path end-to-end
- tests/ passes

## Constraints
- Prefer the smallest change that removes the failure mode.
- Call out any remaining edge cases or follow-up risk explicitly.

## Plan
- Reproduce or localize the failing behavior.
- Implement the minimal targeted fix.
- Run focused regression coverage for the affected behavior.

## PM Sizing
- Complexity: moderate
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
