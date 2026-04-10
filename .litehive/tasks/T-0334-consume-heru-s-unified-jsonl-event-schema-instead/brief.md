# T-0334 Consume heru's unified JSONL event schema instead of per-engine parsing

- Mode: tasks
- Task type: refactor
- PM complexity: complex
- Planned effort: l

## Goal
heru T-0001 shipped a unified JSONL event output schema across all engine adapters (codex, claude, copilot, gemini, opencode, goz). Each engine now emits the same event shape via heru's stream reader. litehive currently has per-engine event parsing scattered across litehive/agents/*. Migrate litehive to read heru's unified event stream directly, deleting the per-engine parsing layers where possible. Keep engine-specific logic only where heru genuinely cannot normalize (e.g. provider-specific quota fields). The public API surface heru exposes is documented in heru's README (shipped in heru T-0006) — use that as the contract. Bump the heru dependency pin to whatever version includes T-0001. Run litehive's full test suite including tests_integration/ after the migration.

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
