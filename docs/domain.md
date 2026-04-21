# Domain Model

This document defines the high-level domain model and canonical terminology for Litehive.

It follows the general format defined in [domain.spec.md](domain.spec.md).

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
- **Entity**: Objects with stable identity that persist over time (`Task`, `SubagentRun`)  
- **Value Object**: Descriptive objects defined by their fields (`TaskRetryPolicy`, `FailureDiagnostics`)
- **Service**: Behavior that coordinates multiple entities (`TaskService`, `PipelineRunner`)
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

**Core Entities**: `Task`, `SubagentRun`, `Session` - objects with stable identity  
**Value Objects**: `TaskRetryPolicy`, `FailureDiagnostics` - descriptive data structures  
**Enums**: `TaskStatus`, `PipelineState`, `StageVerdict` - normalized classification  
**Services**: `TaskService`, `PipelineRunner` - domain behavior coordination  
**Runtime State**: `TaskRuntime`, `PipelineRuntime` - mutable execution tracking

For implementation details, usage patterns, and field-level documentation, consult the docstrings in the corresponding domain module.
