# T-0336 Migrate litehive's heru CLI invocations to positional engine argument form

- Mode: tasks
- Task type: refactor
- PM complexity: simple
- Planned effort: xs

## Goal
heru T-0002 shipped a positional engine argument for the heru CLI: 'heru codex "prompt"' is now the preferred form. Update any place in litehive that shells out to heru (scripts, docs, tests_integration helpers) to use the new positional form instead of any legacy flag-based form. Small mechanical change; bundled as its own task so it does not get lost in the larger T-0001/T-0003 adoption work.

## Acceptance Criteria
- grep for 'heru ' across litehive shows only positional-engine invocations
- tests/ passes
- Any docs/README snippets showing heru CLI usage are updated

## Constraints
- Avoid broad opportunistic cleanup outside the chosen seam.
- Preserve existing behavior unless the task explicitly includes functional changes.

## Plan
- Identify the narrow seam to refactor and the behavior that must stay stable.
- Restructure the code in small, reviewable steps.
- Run focused verification to confirm behavior is preserved.

## PM Sizing
- Complexity: simple
- Planned effort: xs

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
