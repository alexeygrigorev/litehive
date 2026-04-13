# T-0334 Consume heru's unified JSONL event schema instead of per-engine parsing

- Mode: tasks
- Task type: refactor
- PM complexity: complex
- Planned effort: l

## Goal
Refactor Litehive's subagent execution/session pipeline to consume heru's unified JSONL event contract as the source of truth, instead of relying on Litehive-side per-engine JSONL parsing behavior.

## Acceptance Criteria
- litehive/agents/ no longer contains per-engine JSONL parsing code paths that heru's unified schema already covers
- litehive consumes heru's unified event types (imported from heru) for all adapters
- heru dependency pin in litehive's pyproject.toml bumped to a version including T-0001
- tests/ passes in litehive
- tests_integration/ passes in litehive for at least codex + one other engine
- No behavioral regression: nudge flow, verdict submission, and continuation handoff still work end-to-end

## Constraints
- Avoid broad opportunistic cleanup outside the chosen seam.
- Preserve existing behavior unless the task explicitly includes functional changes.

## Plan
- Identify the narrow seam to refactor and the behavior that must stay stable.
- Restructure the code in small, reviewable steps.
- Run focused verification to confirm behavior is preserved.

## PM Sizing
- Complexity: complex
- Planned effort: l

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
