# T-0336 Migrate litehive's heru CLI invocations to positional engine argument form

- Mode: tasks
- Task type: refactor
- PM complexity: simple
- Planned effort: xs

## Goal
Migrate Litehive-owned direct `heru` CLI invocations from the legacy engine-flag form to Heru's preferred positional engine form (`heru <engine> "prompt"`). Scope is limited to places where this repo directly shells out to `heru` or documents that direct invocation in tests/helpers/docs. Litehive's own `litehive ... --engine ...` CLI surface is out of scope.

## Acceptance Criteria
- Every direct `heru` CLI invocation in Litehive-owned code, tests, helpers, and docs uses positional engine syntax; no legacy direct invocation using `--engine` remains.
- Direct Heru CLI coverage preserves existing forwarded behavior for prompt, cwd, model, max-turns, and resume-session-id while using positional engine parsing.
- tests/ passes.
- Any existing README or docs snippets that invoke `heru` directly are updated to the positional form.

## Constraints
- Avoid broad opportunistic cleanup outside the chosen seam.
- Preserve existing behavior unless the task explicitly includes functional changes.
- Do not modify Litehive's own `litehive ... --engine ...` flags, docs, or parser behavior as part of this task.

## Plan
- Find the narrow set of direct `heru` command arrays/snippets and separate them from unrelated `heru` imports and Litehive `--engine` references.
- Update those direct invocations to positional engine syntax in small, reviewable edits while preserving forwarded arguments and runtime behavior.
- Run focused Heru CLI verification, then run the full `tests/` suite and confirm any direct-doc examples are consistent.

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
