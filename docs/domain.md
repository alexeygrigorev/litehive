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

## Core Distinctions

`TaskStatus` is the operator-visible lifecycle category: `queued`,
`in_progress`, `interrupted`, `parked`, `done`, `closed`, or `flagged`.

`PipelineState` is the internal runner state machine. It includes executable
agent stages, hook states, system states, recovery, merge resolution, and
terminal states.

`PipelineStatus` is only a coarse display projection persisted on task records.
It is not the state machine.

`StageReport.pipeline_state` uses the `ReportPipelineState` projection from
`litehive/domain/reports.py`. Stage report verdicts are canonically `pass`,
`reject`, or `blocked`; broader activity verdicts are normalized at the report
boundary.

Close outcomes such as `wont_do`, `deferred`, `duplicate`, and
`execution_cancelled` are close reasons, not task statuses. Merge failures are
represented as flagged tasks with `flag_reason = "merge_failed"`.

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
- `litehive/domain/pool.py`: worktree and dirty-worktree gate reports.
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

## Storage Rule

Task intent, queue state, runtime state, reports, events, monitoring, and audit
records belong in SQLite. The only Litehive-owned YAML file that should remain
in a workspace is `.litehive/config.yaml`; all other structured workspace state
should use the database, JSONL/text logs, or disposable artifact files.

Built-in profile defaults are typed Python package data, not workspace-owned
YAML. Historical workspace YAML belongs in migration or cleanup code only, not
in current domain models.
