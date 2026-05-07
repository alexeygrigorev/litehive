# Domain Model

This document defines Litehive's durable domain vocabulary. Detailed field
semantics belong in the model docstrings under `litehive/domain/`; this file
keeps the terms, ownership boundaries, and storage rules that should stay
stable across refactors.

## Normative Rules

- Use one canonical term per concept.
- Do not expose historical implementation labels such as `v2` in user-facing
  commands, docs, or prompts.
- Avoid overloaded bare words such as `status`, `state`, `reason`, and
  `outcome` when more than one kind is in scope.
- Keep detailed field rationale in code docstrings so it changes with the
  implementation.

## Domain Areas

- **Workspace**: repository registration, workspace configuration, locks,
  backups, and workspace health.
- **Task**: task intent, task records, task lifecycle category, queue
  membership, and operator task transitions.
- **Pipeline**: internal state-machine positions, transition rules, lifecycle
  nodes, hooks, and stage routing.
- **Execution**: runner status, live subagent state, engine continuations,
  interruptions, and runtime projections.
- **Recovery**: failure fingerprints, recovery triggers, recovery outcomes,
  recovery budgets, and failed-run history.
- **Pool**: a runner-level queue drain attempt over workspace tasks. It claims
  eligible queued/resumable work, stops for typed `PoolStopReason` values, and
  leaves an operator summary describing completed, flagged, resumable, closed,
  skipped, and remaining tasks.
- **Reports and Activity**: stage reports, recovery reports, task activity, task
  evidence, audit records, and human-readable history.
- **Artifacts**: raw prompt, event stream, stdout/stderr, execution trace, and
  other support data retained outside the canonical task model.
- **Engine Monitoring**: engine usage observations, quota state, availability,
  freezes, and routing hints.

## Naming Rules

- Use `task` for the core work item.
- Use `pipeline` for the task execution flow.
- Use `pipeline_state` for the internal runner node.
- Use `pipeline_status` for the operator-facing progress projection.
- Use `task_status` for the high-level task lifecycle category.
- Use `reason_code` for normalized machine-readable classification.
- Use `rationale` for operator or agent explanation text.
- Use `message` for human-readable event or report text.

## Core Vocabulary

### Task Lifecycle

- `TaskStatus` is the operator-visible lifecycle category: `queued`,
  `in_progress`, `interrupted`, `parked`, `done`, `closed`, or `flagged`.
- `TaskOutcomeKind` is the terminal bucket for the whole task: done, closed,
  flagged, blocked, interrupted, cancelled, won't-do, deferred, or duplicate.
- `OutcomeReasonCode` is the machine-readable reason that produced a task
  outcome: retry exhaustion, hallucinated completion, operator cancellation,
  missing acceptance criteria, merge conflict, and similar routing facts.
- `TaskCloseReason` is the operator-facing close choice accepted by
  `litehive task close`: `done`, `wont_do`, `deferred`, or `duplicate`.
  It is stored on `TaskRecord.close_reason`; runtime outcome reason codes use
  the broader `task_done` or `task_closed` buckets.
- `execution_interrupted` means execution stopped in a potentially resumable
  way. The runner records interruption context so recovery or resume can pick
  the task up later.
- `execution_cancelled` means an operator deliberately abandoned execution.
  The task is closed/cancelled, not queued for automatic resume.
- Close outcomes such as `wont_do`, `deferred`, `duplicate`, and
  `execution_cancelled` are close reasons, not task statuses.

### Pipeline Progress

- `PipelineState` is the internal runner state machine. It includes executable
  agent stages, hook states, system states, recovery, merge resolution, and
  terminal states.
- `PipelineStatus` is only a coarse display projection persisted on task
  records. It is not the state machine.
- `TaskStage` is the operator-facing work phase used for reports, prompts, and
  retry buckets: grooming, implementing, testing, accepting, or commit-to-git.

### Reports And Verdicts

- `Verdict` describes an agent or hook decision for one stage. It is not a task
  outcome.
- `Verdict.FAIL` is the generic negative verdict kept for older hook and
  activity vocabulary.
- `Verdict.REJECT` is the explicit agent or reviewer decision that a submitted
  stage result is unacceptable.
- `StageReport.pipeline_state` uses the `ReportPipelineState` projection from
  `litehive/domain/reports.py`.
- Stage report verdicts are canonically `pass`, `reject`, or `blocked`;
  broader activity verdicts are normalized at the report boundary.
- Merge failures are represented as flagged tasks with
  `flag_reason = "merge_failed"`.

### Outcome Reason Code Ownership

| Code | Setter | Circumstance |
| --- | --- | --- |
| `verdict_fail` | No current production setter | Reserved historical bucket for a generic failed stage verdict. Current submitted activity rejects unsupported `fail` verdicts. |
| `verdict_reject` | No current production setter | Reserved bucket for a stage report that rejects without a more specific reason. Current routing usually records a failure classification instead. |
| `verdict_blocked` | No current production setter | Reserved bucket for a stage report that blocks without a more specific reason. |
| `hallucinated_completion` | `HeruEngineAdapter` implementing-pass guard | A SWE reports pass with changed files, but the execution checkout is clean. The pass is rewritten to a reject. |
| `missing_acceptance_criteria` | No current production setter | Reserved bucket for the grooming gate that keeps under-specified full-pipeline tasks from entering implementation. Current code emits operator warning text instead. |
| `retry_limit_exhausted` | No current production setter | Reserved bucket for a task-level retry budget failure. |
| `stage_retry_limit_exhausted` | No current production setter | Reserved bucket for a stage-local retry budget failure. |
| `execution_interrupted` | Recovery interruption preparation | Runner or recovery code pauses a task in a resumable state and stores interruption context. |
| `execution_cancelled` | Task abandon transition | An operator abandons an active, interrupted, parked, flagged, or closed task. |
| `stage_exception` | Lifecycle runtime sync | Recovery escalates a stage failure to a follow-up task or records a stage-level exception outcome. |
| `unsupported_verdict` | No current production setter | Reserved bucket for an invalid report verdict. Current CLI and activity models reject unsupported verdicts before persistence. |
| `merge_conflict` | No current production setter | Reserved bucket for merge-conflict outcomes. Current merge conflicts are represented as flagged tasks with `flag_reason = "merge_failed"`. |
| `task_done` | Task close and workspace repair | A task is closed as already satisfied or repaired into a completed runtime shape. |
| `task_closed` | Task close transition | A task is deliberately closed as won't-do, deferred, or duplicate; the specific operator choice remains on `TaskRecord.close_reason`. |

## Recovery Vocabulary

- `FailureFingerprint` is the recovery identity and budget key.
- `failure_diagnostics` is report evidence on stage or outcome records; it is
  not a recovery-domain model.
- `RecoveryTrigger` is the active cause that routed a task into recovery. It is
  persisted as `active_recovery_trigger` and surfaced to recovery prompts as
  `recovery_trigger`.
- `RecoveryOutcome` is one completed recovery attempt or denial. Outcomes are
  persisted in `recovery_history`.
- `RuntimeRecoveryOutcome` is the compact runtime projection used by
  `TaskRuntime.pipeline.recovery_history`.
- `failed_run_history` is separate cross-run retry-exhaustion memory.
- Retired names such as `FailureDiagnostics`, `RecoveryContext`, and
  `RecoveryRecord` should not be reintroduced.

## Domain Modules

- `litehive/domain/common.py`: shared enums, projections, and helpers.
- `litehive/domain/task.py`: task and workspace records.
- `litehive/domain/runtime.py`: runtime, interruption, subagent, and runner
  state models.
- `litehive/domain/recovery.py`: recovery enums and persisted value objects.
- `litehive/domain/reports.py`: stage reports, recovery reports, task activity,
  and report projections.
- `litehive/domain/engine.py`: engine monitoring and live event-stream models.
- `litehive/domain/agent.py`: subagent execution result models and exceptions.
- `litehive/domain/pool.py`: pool stop reasons, pool summary reports,
  worktree reports, and dirty-worktree gate reports.
- `litehive/domain/task_ops.py`: task-operation result and error dataclasses.
- `litehive/domain/lifecycle_deltas.py`: transition deltas and recovery trigger
  construction.
- `litehive/lifecycle/persistence.py`: persisted lifecycle `TaskState`.

## Actors

- **Operator**: human using Litehive through CLI commands.
- **Runner**: top-level process that owns task execution.
- **Pipeline Node**: executable unit for one pipeline state.
- **Subagent**: external agent execution for a specific role.
- **Store**: persistence boundary for structured data.

## Subagent Execution Boundary

`litehive.agents.manager.SubagentManager` is the per-invocation coordinator
for one external subagent process. Its responsibility is to turn a task, role,
engine name, prompt, and execution root into one `SubagentResult` while keeping
the task/runtime/session surfaces coherent.

The manager owns the run-level sequence:

- allocate the subagent id and artifact directory
- create and attach the persisted `Subagent` record
- resolve the engine adapter through `EngineManager`
- apply sandbox wrapping through `SandboxLauncher`
- wire `SubagentRunCallbacks` into engine start/progress callbacks
- classify process exit, interruption, timeout, and startup failures
- record the final stage report snapshot and engine monitoring event

The manager does not own lifecycle routing, prompt policy, engine registry
globals, callback best-effort persistence handling, low-level
session/artifact I/O, sandbox policy calculation, activity storage, or report
parsing. Those remain with `litehive.lifecycle`, `litehive.roles`,
`EngineManager`, `SubagentRunCallbacks`, `SubagentSessionManager`,
`SandboxLauncher`, `Workspace.task_activity(...)`, and
`stage_report_from_subagent(...)` respectively. If new behavior does not fit the
coordinator sequence above, add it to the focused collaborator that owns that
concern instead of widening `SubagentManager`.

## Error Ownership

- `SubagentStartupError` is owned by `litehive.agents.manager.SubagentManager`.
  `SubagentManager.run` raises it only when the engine process has not been
  confirmed started yet: unavailable engine checks, sandbox adapter setup, or
  immediate adapter launch failures before an `on_started` callback or live
  progress reports a pid. After the engine starts, failures are no longer
  startup failures; they are recorded as `EngineFailure` values or propagated
  as their original exception.
- `HeruEngineAdapter.run_turn` is the lifecycle actor that catches
  `SubagentStartupError`. It hands the original exception and formatted startup
  message to `_handle_startup_failure`, which may bypass `SubagentManager` and
  run the recovery actor directly. If that bypass cannot produce a recovery
  verdict, the adapter re-raises the original exception into the normal
  lifecycle crash path.
- `UnexpectedFailureBeforeTheEngineSubprocessStarted` is not a Litehive
  exception class. That phrase describes the `SubagentStartupError` condition:
  an unexpected failure before the external engine subprocess is known to have
  started.

## Storage Rule

Task intent, queue state, runtime state, reports, events, monitoring, and audit
records belong in SQLite. The only Litehive-owned YAML file that should remain
in a workspace is `.litehive/config.yaml`; all other structured workspace state
should use the database, JSONL/text logs, or disposable artifact files.

Built-in profile defaults are typed Python package data, not workspace-owned
YAML. Historical workspace YAML belongs in migration or cleanup code only, not
in current domain models.
