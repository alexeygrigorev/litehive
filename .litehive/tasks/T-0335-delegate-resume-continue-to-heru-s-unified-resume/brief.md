# T-0335 Delegate --resume/--continue to heru's unified resume mechanism

- Mode: tasks
- Task type: refactor
- PM complexity: moderate
- Planned effort: m

## Goal
heru T-0003 shipped a unified --resume/--continue path across all engine adapters. litehive currently has per-engine resume/continuation logic (see extract_engine_continuation and friends in litehive/agents/). Replace that with a single call into heru's unified resume API. Delete the per-engine continuation extractors where heru now covers them. Preserve litehive's RuntimeContinuationHandoff semantics — only the low-level 'how do I hand a session id back to engine X' layer changes. Bump the heru dependency pin to a version including T-0003.

## Acceptance Criteria
- litehive/agents/ no longer contains per-engine resume/continuation extraction logic that heru's unified API covers
- extract_engine_continuation (or equivalent) delegates to heru for all supported engines
- RuntimeContinuationHandoff behavior is unchanged from the caller's perspective
- heru dependency pin bumped to a version including T-0003
- tests/ passes
- tests_integration/ resume/continuation tests pass for codex and at least one other engine

## Constraints
- Avoid broad opportunistic cleanup outside the chosen seam.
- Preserve existing behavior unless the task explicitly includes functional changes.

## Plan
- Identify the narrow seam to refactor and the behavior that must stay stable.
- Restructure the code in small, reviewable steps.
- Run focused verification to confirm behavior is preserved.

## PM Sizing
- Complexity: moderate
- Planned effort: m

## Template Guidance
- Name the seam being refactored and the behavior that must not change.
- Keep the scope structural unless the task explicitly includes functional change.
- Use focused verification to prove behavior stayed stable.

## Intake Notes

### Refactor Seam
- Identify the module, function, or flow being reshaped.

_TBD_

### Behavior to Preserve
- List the user-visible or contract-level behavior that must stay the same.

_TBD_

### Verification
- Capture the checks that confirm the refactor did not regress behavior.

_TBD_
