# Domain Document Spec

This document describes how domain documentation should be organized. It serves as a template for writing domain model documentation.

**NOTE**: Detailed domain model explanations for Litehive have been moved from this documentation into code docstrings. This spec now focuses on structural guidelines for domain documentation.

## Domain Documentation Philosophy

Modern domain documentation should balance overview clarity with implementation detail. The preferred approach is:

- **High-level documentation**: Provides conceptual overview, domain organization, and design principles
- **Code-embedded documentation**: Places detailed usage patterns, creation contexts, and field explanations directly in code docstrings

## Core Principles

### One Canonical Term
Each important concept should have one preferred name. Avoid multiple aliases or overloaded terms.

### Domain Grouping  
Group related concepts by domain for coherent understanding.

### Purpose Over Shape
Explain why concepts exist, not just their structure.

### Live Documentation
Embed detailed rationale in code where it stays current and discoverable.

## Implementation Guidelines

### Documentation Organization
- **Overview docs**: High-level domain organization and principles
- **Code docstrings**: Detailed model usage, creation contexts, and field explanations
- **Inline comments**: Non-obvious field meanings and population contexts

### Essential Information
Each domain model should document:
- Purpose and creation context (in class docstring)
- Field meanings and usage patterns (in field comments/metadata)  
- Actor responsibilities and workflows (in service/store docstrings)

## Modern Documentation Approach

### Code-First Documentation
The preferred approach is to embed detailed domain explanations directly in code:
- **Class docstrings** explain model purpose and primary consumers
- **Field comments/metadata** explain population context and usage patterns  
- **Service docstrings** document actor responsibilities and workflows

### Naming Conventions
- Class names: `PascalCase` nouns (`Task`, `PipelineState`)
- Enum names: `PascalCase` classifiers (`TaskStatus`, `FailureReasonCode`)  
- Field names: `snake_case` with explicit names (`close_reason`, `pipeline_state`)
- Timestamps: `_at` suffix (`created_at`, `updated_at`)
- IDs: `_id` suffix (`task_id`, `subagent_id`)

### Model Type Selection
- **Pydantic models**: For persistent records, API boundaries, validation needs (`Task`, `SubagentRun`)
- **Dataclasses**: For transient events, lightweight in-memory objects (`AcceptEvent`, `RejectEvent`)  
- **Frozen dataclasses**: For immutable events representing completed facts

This approach keeps domain knowledge close to implementation while maintaining high-level conceptual overview in documentation.
