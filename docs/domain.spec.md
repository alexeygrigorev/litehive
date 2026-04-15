# Domain Document Spec

This document describes how a domain document should be written in general.
It is not specific to Litehive. Use it as a template when writing or reviewing
`domain.md` files for any project.

## Contents

- [Goal](#goal)
- [Audience](#audience)
- [Core Principles](#core-principles)
- [Recommended Document Structure](#recommended-document-structure)
- [Entry Ordering Rules](#entry-ordering-rules)
- [Required vs Optional Sections](#required-vs-optional-sections)
- [Domain Section Template](#domain-section-template)
- [Entry Types](#entry-types)
- [Per-Type Templates](#per-type-templates)
- [When To Split or Merge Concepts](#when-to-split-or-merge-concepts)
- [When To Create a New Domain](#when-to-create-a-new-domain)
- [Relationship Notation](#relationship-notation)
- [Naming Conventions For Code](#naming-conventions-for-code)
- [What Good Entries Answer](#what-good-entries-answer)
- [Choosing Pydantic vs Dataclass](#choosing-pydantic-vs-dataclass)
- [Anti-Patterns](#anti-patterns)
- [Good vs Bad Entry Examples](#good-vs-bad-entry-examples)
- [Open Questions Format](#open-questions-format)
- [Migration Note Format](#migration-note-format)
- [Boundary Rules](#boundary-rules)
- [Completeness Checklist Per Domain](#completeness-checklist-per-domain)
- [What To Avoid](#what-to-avoid)
- [Style Rules](#style-rules)
- [Review Checklist](#review-checklist)
- [Applying This Spec](#applying-this-spec)

## Goal

A domain document should do three things at once:

- define one canonical term per important concept
- explain why the concept exists in the system
- show who creates, sets, reads, and acts on it

If a reader can only learn the names but still cannot answer "why is this
thing here?" or "who uses it?", the document is incomplete.

## Audience

A good domain document should work for three audiences:

- product or domain readers who need the conceptual model
- engineers who need target code names and boundaries
- reviewers who need to spot duplication, overloaded terms, and unclear
  responsibilities

## Core Principles

### One Canonical Term

Each important concept should have one preferred name.

Avoid:

- multiple casual aliases for the same thing
- historical names mixed with current target names
- bare overloaded words such as `status`, `state`, `reason`, or `event`

### Domain Grouping

Group related concepts by domain so the reader can review a coherent slice of
the model without jumping around the file.

Typical domain section content:

- what the domain is for
- who acts inside that domain
- what actions happen there
- which entities, value objects, enums, stores, and services belong there

What should be inside a domain:

- concepts that exist for the same business or product purpose
- the actors that do work primarily inside that domain
- the actions that matter inside that domain
- the entities, value objects, enums, services, stores, and artifacts that are
  mainly owned by that domain

What should stay out of a domain section:

- global modeling terminology
- concepts that are reused across many domains and do not belong to one of
  them specifically
- repeated explanations that are better stated once in a cross-domain section

### Explain Purpose, Not Just Shape

Do not stop at:

- a name
- a type
- a code snippet

Also explain:

- why the thing exists
- who creates or sets it
- who reads or mutates it after creation
- what decisions it supports

### Prefer Target Model Over Accidental Current Shape

The document should describe the intended domain model, not just mirror today's
implementation debris.

It may reference current code when useful, but it should not let current code
confusion dictate the vocabulary.

## Recommended Document Structure

Use this high-level shape:

1. Purpose of the document
2. General modeling terms and conventions
3. Naming rules
4. Domain sections
5. Review checklist or open questions if needed

## Entry Ordering Rules

Within one domain, prefer this order:

1. enums and classifiers
2. small value objects
3. entities and durable records
4. services and stores
5. artifacts specific to that domain

Why:

- readers usually need the vocabulary labels first
- then the small descriptive objects
- then the main records
- then the behavior and persistence boundaries around them

If a different order is clearly better for readability in one domain, use it
deliberately and keep it internally consistent.

## Required vs Optional Sections

Every entry should clearly indicate which information is required versus
optional.

Required:

- plain-language definition
- canonical term
- target Python type
- ownership context such as `Created by` or `Set by`
- consumer context such as `Used by` or `Used after creation by`

Optional:

- code sketch
- field meanings
- invariants
- lifecycle notes
- examples
- migration notes

If an optional section is omitted, the entry should still answer what it is,
why it exists, who creates or sets it, and who uses it.

## Domain Section Template

Each domain section should start with:

- `Purpose`
- `Primary actors`
- `Primary actions`

Example:

~~~md
## Payment Domain

Purpose:
this domain exists to model payment initiation, authorization, settlement, and
refund behavior.

Primary actors:

- `Customer`
- `PaymentService`
- `PaymentGateway`

Primary actions:

- create a payment
- authorize a payment
- capture a payment
- refund a payment
~~~

## Entry Types

Not every entry should use the exact same template. The template should match
the kind of thing being described.

## Per-Type Templates

Use the following templates as the default shapes for entries in a domain
document.

### Domain Template

Use for top-level domain sections.

Template:

~~~md
## <Domain Name> Domain

Purpose:
<why this domain exists>

Primary actors:

- `<ActorOrService>`: <what it does here>

Primary actions:

- <meaningful domain action>
~~~

### Actor Template

Use for humans, processes, services, adapters, or external systems that do
work in the model.

Template:

~~~md
### <Actor Name>

<one-sentence definition>

Exists because:

- <why this actor is a distinct actor in the model>

Uses:

- <main concepts this actor reads or writes>
~~~

### Cross-Domain Section Template

Use for concepts that are intentionally shared across the whole model instead
of belonging to one domain.

Typical examples:

- general modeling terms
- naming rules
- cross-domain actors
- document-wide conventions

Template:

~~~md
## <Cross-Domain Section Name>

<why this section is global rather than domain-local>

### <Shared Concept>

<one-sentence definition>

Exists because:

- <why the concept cuts across domains>

Used by:

- <which domains or actors rely on it>
~~~

## When To Split or Merge Concepts

Create a separate concept when:

- it has its own lifecycle or identity
- different actors create and use it for different reasons
- it has meaning outside one parent object field
- the code and docs become clearer when it is named explicitly

Keep it as a field on an existing concept when:

- it has no useful meaning on its own
- it only adds one trivial field to a subtype
- it is only ever used together with the parent object
- separating it would add vocabulary without adding clarity

Signals that a concept should be merged:

- a subtype adds only one field and no distinct behavior
- two enums differ only by one or two duplicated values
- readers keep asking why two names exist

Signals that a concept should be split:

- one object mixes durable state, transient runtime state, and reports
- the same field group is repeatedly passed around together
- the same concept is being described differently in multiple sections

## When To Create a New Domain

Create a new domain when:

- a coherent set of concepts exists for its own product purpose
- that set has its own actors, actions, and rules
- keeping it inside another domain makes the parent domain too broad

Do not create a new domain when:

- the concepts are just one small corner of an existing domain
- the only reason is implementation folder layout
- the new domain would contain only one or two weakly justified entries

### Entity or Record Template

Use this for objects with identity that persist over time.

Required fields:

- short plain-language definition
- `Created by`
- `Exists because` or `Created for`
- `Used after creation by`
- preferred name
- Python type
- code sketch

Optional fields:

- field meanings
- invariants
- lifecycle notes

Example:

~~~md
### Order

A customer purchase tracked over time.

Created by:

- `CheckoutService` when a checkout is submitted

Exists because:

- the system needs one durable entity representing a purchase across its full
  lifecycle

Used after creation by:

- `PaymentService`, `FulfillmentService`, and operator tooling

- Preferred: `order`
- Python type: `Order`

```python
class Order(BaseModel):
    id: str
    status: OrderStatus
```
~~~

### Value Object Template

Use this for descriptive objects that do not have their own identity.

Required fields:

- short definition
- `Created by` or `Derived by`
- `Exists because`
- `Used after creation by`
- preferred name
- Python type
- code sketch

Template:

~~~md
### <Value Object Name>

<one-sentence definition>

Created by:

- `<ActorOrService>` when <value is derived or assembled>

Exists because:

- <why this descriptive object is worth naming explicitly>

Used after creation by:

- `<ActorOrService>` to <decision or behavior>

- Preferred: `<canonical term>`
- Python type: `<TypeName>`

```python
class <TypeName>(BaseModel):
    ...
```
~~~

### Enum, Status, Reason, or Classifier Template

Use this for enums and other normalized labels.

Required fields:

- short definition
- `Set by`
- `Used by`
- preferred name
- Python type
- code sketch
- values with short meanings when helpful

Example:

~~~md
### Order Status

The high-level lifecycle state for an order.

Set by:

- `CheckoutService`, `PaymentService`, and `FulfillmentService`

Used by:

- routing logic, operator views, and reporting

- Preferred: `order status`
- Python type: `OrderStatus`
~~~

Template:

~~~md
### <Enum Name>

<one-sentence definition>

Set by:

- `<ActorOrService>` when <the value is assigned>

Used by:

- `<ActorOrService>` to <decision, filtering, or reporting behavior>

- Preferred: `<canonical term>`
- Python type: `<TypeName>`

```python
class <TypeName>(str, Enum):
    ...
```

Values:

- `<value>`: <meaning>
~~~

### Service Template

Use this for domain services and coordinators.

Required fields:

- short definition
- `Exists because`
- `Used by`
- preferred name
- Python type
- code sketch

Optional fields:

- responsibilities
- what this service intentionally does not own

Template:

~~~md
### <Service Name>

<one-sentence definition>

Exists because:

- <why this behavior should not live on one entity>

Used by:

- `<ActorOrService>` to <main responsibilities>

- Preferred: `<canonical term>`
- Python type: `<TypeName>`

```python
class <TypeName>:
    ...
```
~~~

### Store Template

Use this for persistence boundaries.

Required fields:

- short definition
- `Exists because`
- `Used by`
- preferred name
- Python type
- code sketch

Template:

~~~md
### <Store Name>

<one-sentence definition>

Exists because:

- <why persistence should be hidden behind a boundary>

Used by:

- `<ActorOrService>` to load and save structured objects

- Preferred: `<canonical term>`
- Python type: `<TypeName>`

```python
class <TypeName>(ABC):
    ...
```
~~~

### Event Template

Use this for typed events.

Required fields:

- short definition
- `Created by`
- `Exists because`
- `Used after creation by`
- preferred name
- Python type
- code sketch

Template:

~~~md
### <Event Name>

<one-sentence definition>

Created by:

- `<ActorOrService>` when <something happens>

Exists because:

- <why the system needs a typed event instead of direct branching everywhere>

Used after creation by:

- `<ActorOrService>` to <routing or follow-up behavior>

- Preferred: `<canonical term>`
- Python type: `<TypeName>`

```python
@dataclass(frozen=True)
class <TypeName>:
    ...
```
~~~

### Artifact Template

Use for persisted byproducts such as traces, journals, logs, and sessions.

Template:

~~~md
### <Artifact Name>

<one-sentence definition>

Created by:

- `<ActorOrService>` when <artifact is emitted>

Exists because:

- <why this byproduct is kept separately from core runtime state>

Used after creation by:

- `<ActorOrService>` for debugging, auditing, replay, or inspection

- Preferred: `<canonical term>`
- Python type: `<TypeName>`

```python
class <TypeName>(BaseModel):
    ...
```
~~~

## Relationship Notation

When describing how concepts relate, use short explicit phrases such as:

- `owned by`
- `references`
- `created by`
- `set by`
- `used by`
- `persists to`
- `derived from`
- `resumed by`
- `reported by`

Avoid vague relationship language such as:

- "connected to"
- "involved with"
- "linked somehow"

The relationship wording should help the reader understand direction and
responsibility.

## Naming Conventions For Code

Use these defaults unless a stronger project-specific convention exists.

Class names:

- nouns in `PascalCase`
- examples: `Task`, `PipelineState`, `RecoveryTrigger`

Enum names:

- singular nouns or classifiers in `PascalCase`
- examples: `TaskStatus`, `FailureReasonCode`

Field names:

- `snake_case`
- prefer explicit names such as `close_reason`, `pipeline_state`,
  `trigger_event_kind`

Boolean field names:

- prefer `is_`, `has_`, `can_`, or a clear adjective
- examples: `is_blocking`, `has_conflict`, `can_resume`

Timestamp field names:

- use suffixes such as `_at`
- examples: `created_at`, `updated_at`, `interrupted_at`

Identifier field names:

- use `_id` for identifiers and `_ids` for lists of identifiers
- examples: `task_id`, `subagent_id`, `queued_task_ids`

Reason and classifier fields:

- prefer `reason_code` for normalized machine-readable labels
- prefer `message` for human-readable text
- prefer `rationale` for explanation of a deliberate choice

## What Good Entries Answer

A good entry should let the reader answer these questions without leaving the
section:

- What is it?
- Why does it exist?
- Who creates or sets it?
- Who uses it later?
- What is the canonical name?
- What should the target code shape look like?

## Choosing Pydantic vs Dataclass

The domain document should say not only the target type name, but also the
kind of Python model that best fits it.

### Use a Pydantic Model When

Use `BaseModel` when the object:

- is loaded from or saved to storage
- crosses process, CLI, or API boundaries
- needs parsing, validation, coercion, or serialization
- is part of a durable record or report
- contains nested structured data where validation matters

Typical examples:

- entities and records such as `Task`, `SubagentRun`, `Session`
- value objects stored inside those records
- reports, diagnostics, configuration, and persisted artifacts

Why:

- Pydantic gives validation, nested parsing, defaults, and straightforward
  dump/load behavior

### Use a Dataclass When

Use `@dataclass` when the object:

- is a small in-memory message or event
- is transient rather than a storage schema
- benefits from cheap construction and explicit field layout
- should stay lightweight and mostly validation-free

Typical examples:

- runtime events such as `AcceptEvent`, `RejectEvent`, `CrashEvent`
- small internal command or signal objects

Why:

- dataclasses fit short-lived in-memory facts well and keep event types simple

### Prefer Frozen Dataclasses For Events

For event objects, prefer:

```python
@dataclass(frozen=True)
class SomeEvent:
    ...
```

Reason:

- events represent facts that already happened
- immutability reduces accidental mutation during routing

### Prefer Pydantic For Durable Domain Records

For anything that is likely to be:

- saved to sqlite or another store
- embedded in a persisted object
- emitted through a CLI or API boundary

prefer:

```python
class SomeRecord(BaseModel):
    ...
```

### Avoid Mixing The Roles

Avoid using:

- dataclasses for persistence-heavy records that need validation
- Pydantic models for tiny ephemeral events unless there is a strong boundary
  reason

Default rule:

- durable structured record -> `BaseModel`
- transient in-memory signal -> `@dataclass`

## Anti-Patterns

Watch for these common domain-document failures:

- duplicate enums that restate the same concepts with slightly different names
- subtypes that add only one trivial field
- current-code dumps with no target-model explanation
- vague actors such as "the system"
- artifacts duplicated in core records even though they are derivable elsewhere
- one domain section silently mixing multiple unrelated concerns
- entries that show code but never explain why the concept exists

## Good vs Bad Entry Examples

Weak entry:

~~~md
### PaymentState

```python
class PaymentState(str, Enum):
    NEW = "new"
    DONE = "done"
```
~~~

Why it is weak:

- it gives no reason for the concept
- it does not say who sets it
- it does not say who uses it
- it may duplicate another state enum without making that obvious

Stronger entry:

~~~md
### Payment Status

The high-level lifecycle state for a payment.

Set by:

- `CheckoutService` when a payment starts
- `SettlementService` when a payment completes

Used by:

- routing, operator views, and reporting

- Preferred: `payment status`
- Python type: `PaymentStatus`
~~~

Why it is stronger:

- it defines the concept in plain language
- it explains ownership
- it explains use
- it gives a clear canonical name

## Open Questions Format

If something is unresolved, keep it out of the canonical entry body whenever
possible.

Preferred format:

~~~md
## Open Questions

- Should `PaymentStatus` and `PaymentOutcome` stay separate?
- Should `RefundRequest` be its own entity or part of `Payment`?
~~~

Rule:

- unresolved design questions should be collected in one explicit place instead
  of being scattered as inline comments

## Migration Note Format

If the document is guiding an active rename or refactor, record migrations in a
small, explicit section.

Template:

~~~md
## Migration Notes

- current `PaymentStateRecord` -> target `Payment`
- current `payment_status` -> target `payment_state`
~~~

Rule:

- migration notes are transitional
- they should not dilute the canonical vocabulary sections themselves

## Boundary Rules

Domain documents should define:

- canonical names
- target domain concepts
- actor and ownership context
- target code-shape sketches when useful

Domain documents should not become:

- full architecture documents
- full API references
- full CLI references
- full database schemas

If a concept needs deeper runtime flow explanation, link or defer to
architecture, pipeline, API, or CLI documentation rather than overloading the
domain document.

## Completeness Checklist Per Domain

For each domain, ask whether it has clearly defined:

- purpose
- actors
- actions
- enums or classifiers
- value objects
- entities or records
- services
- stores
- artifacts if relevant
- cross-domain dependencies when relevant

## What To Avoid

Avoid entries that are only:

- a raw code block with no explanation
- a prose paragraph with no canonical type
- a name plus synonyms but no decision
- a current-code dump that does not explain intent

Avoid documents that:

- mix unrelated domains together
- use inconsistent section templates
- describe every concept at a different level of detail with no pattern
- leave the reader guessing who owns a concept

## Style Rules

- Prefer short, explicit sentences over abstract architecture language.
- Name the actor or service directly instead of saying "the system".
- Use `Exists because` when explaining why a concept is in the model.
- Use `Created by` for entities, records, events, and value objects that are
  produced.
- Use `Set by` for enums, statuses, flags, and classifiers.
- Use `Used after creation by` for durable objects and `Used by` for enums and
  services.
- Put the plain-language explanation before the code sketch.
- Keep the code sketch minimal; it should teach the shape, not every field in
  the implementation.

## Review Checklist

When reviewing a domain document, ask:

- Does every important concept have one canonical name?
- Are domains grouped coherently?
- Does each entry explain why it exists?
- Is it clear who creates or sets it?
- Is it clear who uses it later?
- Do services and stores have justified boundaries?
- Are duplicate enums or near-duplicate concepts visible?
- Is the target model clear even if current code is messy?

## Applying This Spec

When adapting an existing domain document to this spec:

1. Group entries by domain first.
2. Normalize names second.
3. Add purpose, creator, and consumer context third.
4. Only then refine code sketches and field details.

That order matters. Without it, the document becomes a glossary of types
instead of a readable domain model.
