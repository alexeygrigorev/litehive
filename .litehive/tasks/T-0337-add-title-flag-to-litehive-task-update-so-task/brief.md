# T-0337 Add --title flag to 'litehive task update' so task titles can be renamed in place

- Mode: tasks
- Task type: adapter
- PM complexity: simple
- Planned effort: xs

## Goal
'litehive task update' should support renaming a task's title in place. Today there is no way to change a title after creation, which forces close-and-recreate workflows that burn task ids and fragment history. The task id must be preserved across the rename.

## Acceptance Criteria
- 'litehive task update <id> --title "new title"' replaces the stored title
- 'litehive task show <id>' reflects the new title
- Task id is preserved across the rename
- tests/ passes

## Constraints
- Keep provider-specific behavior isolated to the adapter boundary.
- Preserve deterministic workspace state and execution flow.

## Plan
- Inspect the existing adapter interface, config wiring, and invocation flow.
- Implement the adapter change close to the integration seam.
- Verify the adapter path with a focused test or representative run.

## PM Sizing
- Complexity: simple
- Planned effort: xs

## Template Guidance
- State the target adapter seam, external dependency, and expected contract up front.
- Call out config, invocation, and failure-path changes explicitly.
- Prefer verification that exercises the adapter boundary rather than unrelated paths.

## Intake Notes

### Adapter Surface
- Identify the entrypoint, inputs, outputs, and external system involved.

_TBD_

### Config and Execution Path
- Note which settings, command wiring, or failure handling must change.

_TBD_

### Verification Evidence
- Capture the focused run or test that proves the adapter path works.

_TBD_
