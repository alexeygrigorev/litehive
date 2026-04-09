# T-0276 goz adapter: make Litehive honor goz model overrides

- Mode: tasks
- Task type: adapter
- PM complexity: simple
- Planned effort: s

## Goal
Update Litehive's goz adapter so workspace and task model overrides are passed through to goz run.

## Acceptance Criteria
- GozCLIAdapter advertises supports_model_override=True
- Litehive passes --model to goz run when a model is resolved
- Focused tests cover goz model override resolution and command building

## Constraints
- Keep provider-specific behavior isolated to the adapter boundary.
- Preserve deterministic workspace state and execution flow.

## Plan
- Inspect the existing adapter interface, config wiring, and invocation flow.
- Implement the adapter change close to the integration seam.
- Verify the adapter path with a focused test or representative run.

## PM Sizing
- Complexity: simple
- Planned effort: s

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
