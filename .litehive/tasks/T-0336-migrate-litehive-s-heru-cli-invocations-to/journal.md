# T-0336 Migrate litehive's heru CLI invocations to positional engine argument form

## 2026-04-10T20:48:22+00:00
Task created.

## 2026-04-12T13:58:19+00:00
Task record updated from grooming output:
- goal: `Migrate Litehive-owned direct `heru` CLI invocations from the legacy engine-flag form to Heru's preferred positional engine form (`heru <engine> "prompt"`). Scope is limited to places where this repo directly shells out to `heru` or documents that direct invocation in tests/helpers/docs. Litehive's own `litehive ... --engine ...` CLI surface is out of scope.`
- acceptance_criteria: `['Every direct `heru` CLI invocation in Litehive-owned code, tests, helpers, and docs uses positional engine syntax; no legacy direct invocation using `--engine` remains.', 'Direct Heru CLI coverage preserves existing forwarded behavior for prompt, cwd, model, max-turns, and resume-session-id while using positional engine parsing.', 'tests/ passes.', 'Any existing README or docs snippets that invoke `heru` directly are updated to the positional form.']`
- constraints: `['Avoid broad opportunistic cleanup outside the chosen seam.', 'Preserve existing behavior unless the task explicitly includes functional changes.', "Do not modify Litehive's own `litehive ... --engine ...` flags, docs, or parser behavior as part of this task."]`
- plan: `['Find the narrow set of direct `heru` command arrays/snippets and separate them from unrelated `heru` imports and Litehive `--engine` references.', 'Update those direct invocations to positional engine syntax in small, reviewable edits while preserving forwarded arguments and runtime behavior.', 'Run focused Heru CLI verification, then run the full `tests/` suite and confirm any direct-doc examples are consistent.']`
