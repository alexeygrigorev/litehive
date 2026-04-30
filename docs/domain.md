# Domain Model

This document defines the high-level domain model and canonical terminology for Litehive.

It follows the general format defined in [domain-spec.md](domain-spec.md).

**NOTE**: Detailed rationale, usage patterns, and field explanations for domain models have been moved into code docstrings and field annotations. See the domain classes in `litehive/domain/` for comprehensive documentation of when/how/why each model is used.

## Normative Rules

- use one canonical term per concept
- do not use `v2` in user-facing language
- do not use bare overloaded words like `status`, `state`, `reason`, or `outcome` when more than one kind is in scope
- group related concepts by domain so review is local, not scattered

## Domain Overview

Litehive's domain model is organized into the following domains:

- **Workspace Domain**: workspace-scoped coordination and task queue management
- **Task Domain**: core work items and their lifecycle states  
- **Pipeline Domain**: task execution flow and state transitions
- **Recovery Domain**: failure handling and recovery coordination
- **Execution Domain**: live execution state and subagent management
- **Activity Domain**: human-readable task history and reporting
- **Artifacts Domain**: persisted execution byproducts and traces
- **Configuration Domain**: operator-controlled workspace settings

For detailed domain model documentation including creation context, usage patterns, and field explanations, see the corresponding modules in `litehive/domain/`.

## Modeling Terms

Litehive uses lightweight Domain-Driven Design terminology. Key concepts include:

- **Domain**: A coherent area of the model (workspace, task, pipeline, etc.)
- **Entity**: Objects with stable identity that persist over time (`TaskRecord`,
  subagent runtime/session records)
- **Value Object**: Descriptive objects defined by their fields (`FailureFingerprint`, `RecoveryTrigger`)
- **Service**: Behavior that coordinates multiple entities (task transition
  operations, state-machine runner, recovery coordination)
- **Store**: Persistence boundaries (`WorkspaceStore`, `ArtifactStore`)
- **Actor**: People or components that do work (`Operator`, `Runner`, `Subagent`)
- **Event**: Typed facts about what happened (`AcceptEvent`, `RejectEvent`)

**Ubiquitous Language Rule**: Code, docs, CLI, and discussions should use the same terms (e.g., if code says `PipelineState`, don't casually call it `phase` or `status`).

## Naming Rules

- Use `task` for the core work item
- Use `pipeline` for the task execution flow  
- Use `message` for human-readable text
- Use `reason_code` for normalized machine-readable classification
- Use `rationale` for operator or agent explanation of a choice

## Recovery Vocabulary

The recovery domain uses the implemented recovery model as canonical:

- `FailureFingerprint` is the recovery identity and budget key. It replaces the older document-only `FailureDiagnostics` model name.
- `failure_diagnostics` is a report/outcome evidence field on `StageReport` and `TaskOutcomeState`; it is not a recovery-domain model.
- `RecoveryTrigger` is the active recovery cause/context. It is stored as `TaskState.active_recovery_trigger`, serialized as `active_recovery_trigger`, and surfaced to recovery prompts as `recovery_trigger`.
- `RecoveryOutcome` is one completed recovery attempt or denial. It is stored in `TaskState.recovery_history`.
- `RuntimeRecoveryOutcome` is the compact task-runtime projection of `RecoveryOutcome`, stored in `TaskRuntime.pipeline.recovery_history` so recovery prompts can retain prior fingerprints after state resets.
- `failed_run_history` is separate cross-run retry-exhaustion memory; it is not a recovery outcome.
- `RecoveryContext` and `RecoveryRecord` are retired names. New code and docs should use `RecoveryTrigger`, `RecoveryOutcome`, `recovery_trigger`, and `recovery_history`.

## Cross-Domain Actors

Key actors that operate across multiple domains:

- **Operator**: Human using Litehive through CLI commands
- **Runner**: Top-level process orchestrating task execution  
- **Pipeline Node**: Executable unit for one pipeline state
- **Subagent**: External agent execution for specific roles (planning, implementation, QA, etc.)
- **Store**: Persistence boundary for structured data

*See `litehive/domain/` for detailed actor definitions and responsibilities.*

## Domain Model Reference

The complete domain model documentation with detailed usage patterns, creation contexts, and field explanations has been moved to **code docstrings** in the domain modules. 

Each domain module contains comprehensive docstrings for all models, including:
- Purpose and creation context  
- Field-level usage explanations
- Actor responsibilities and usage patterns
- When/how/why each model is populated and consumed

### Domain Modules

- **`litehive/domain/common.py`** - Shared enums and utilities across all domains
- **`litehive/domain/task.py`** - Task entities and task lifecycle models  
- **`litehive/domain/engine.py`** - Pipeline execution flow and state management
- **`litehive/domain/recovery.py`** - Failure handling and recovery coordination
- **`litehive/domain/runtime.py`** - Live execution state and subagent management
- **`litehive/domain/reports.py`** - Human-readable activity and structured reports
- **`litehive/domain/agent.py`** - Subagent execution and artifact persistence  
- **`litehive/lifecycle/persistence.py`** - Task state persistence (TaskState class)

### Key Model Categories

**Core Entities**: `TaskRecord`, subagent session/runtime records - objects with stable identity
**Value Objects**: `TaskRetryPolicy`, `FailureFingerprint`, `RecoveryTrigger`, `RecoveryOutcome` - descriptive data structures
**Enums**: `TaskStatus`, `PipelineState`, `PipelineStatus`, `Verdict` - normalized classification

`PipelineState` is the internal runner state machine. `PipelineStatus` is only
the operator-facing progress projection persisted on task records for display
and filtering. `StageReport.pipeline_state` uses the named
`ReportPipelineState` projection documented in `litehive/domain/reports.py`.
Stage report verdicts are canonically `pass`, `reject`, or `blocked`; broader
activity verdicts are normalized into that set at the report boundary.

**Services**: task transition operations, `StateMachineRunner`, recovery coordination - domain behavior coordination
**Runtime State**: `TaskRuntime`, `PipelineRuntime`, `ExecutionRuntime` - mutable execution tracking

## Storage Rule

Task intent, queue state, runtime state, reports, events, monitoring, and audit
records belong in SQLite. The only LiteHive-owned YAML file that should remain
in a workspace is `.litehive/config.yaml`; all other structured workspace state
should use the database or append-only text/JSONL artifacts when they are
intentionally logs.

Built-in profile defaults are typed Python package data, not YAML files or
workspace-owned runtime files. When old workspace YAML is found under
`.litehive`, Litehive first creates a compressed database backup, moves the
YAML outside `.litehive`, and then removes the workspace copies.

For implementation details, usage patterns, and field-level documentation, consult the docstrings in the corresponding domain module.
