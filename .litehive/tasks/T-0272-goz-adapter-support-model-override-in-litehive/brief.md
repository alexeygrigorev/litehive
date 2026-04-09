# T-0272 goz adapter: support model override in Litehive

- Mode: tasks
- Task type: adapter
- PM complexity: simple
- Planned effort: s

## Goal
Update Litehive's goz adapter contract so task/workspace model selection is honored when running goz.

## Acceptance Criteria
- GozCLIAdapter advertises supports_model_override=True
- GozCLIAdapter.build_command passes --model through to goz run when provided
- resolve_model returns the workspace/task override for goz tasks
- Add or update tests covering goz model override behavior

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
