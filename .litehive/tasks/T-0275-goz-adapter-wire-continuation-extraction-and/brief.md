# T-0275 goz adapter: wire continuation extraction and resume-session

- Mode: tasks
- Task type: adapter
- PM complexity: moderate
- Planned effort: m

## Goal
Update Litehive's goz adapter to extract continuation metadata from goz JSONL output and resume goz sessions on retries or recovery.

## Acceptance Criteria
- GozCLIAdapter extracts RuntimeEngineContinuation from step_finish continuation/session fields
- Litehive passes --resume-session to goz run when resume_session_id is available
- Focused tests cover goz continuation extraction and resume command building

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
