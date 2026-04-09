# T-0271 goz adapter: extract continuation and wire resume-session

- Mode: tasks
- Task type: adapter
- PM complexity: moderate
- Planned effort: m

## Goal
Bring goz continuation handling up to parity with the other JSONL executors by parsing continuation metadata and passing resume IDs back into goz run.

## Acceptance Criteria
- GozCLIAdapter extracts RuntimeEngineContinuation from goz JSONL output
- GozCLIAdapter.build_command passes --resume-session when Litehive provides resume_session_id
- Crash/retry resume paths can reuse goz session IDs
- Add tests for goz continuation extraction and resume command building

## Constraints
- Keep provider-specific behavior isolated to the adapter boundary.
- Preserve deterministic workspace state and execution flow.

## Plan
- Inspect the existing adapter interface, config wiring, and invocation flow.
- Implement the adapter change close to the integration seam.
- Verify the adapter path with a focused test or representative run.

## PM Sizing
- Complexity: moderate
- Planned effort: m

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
