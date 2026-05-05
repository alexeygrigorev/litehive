# AGENTS.md

Guidance for AI coding agents working in this repository.

## Authoritative style and feedback

- **Code style:** [`docs/code-style.md`](docs/code-style.md) is the
  source of truth for project conventions — imports, control flow,
  domain values, module organization, classes vs free functions,
  prompts, defensive coding, and test discipline. Read it before
  making non-trivial changes.
- **Reviewer feedback:** [`docs/feedback-2026-05-03.md`](docs/feedback-2026-05-03.md)
  captures the original voice-note feedback (R1–R12) that drove the
  current style rules. Use it when you need the *why* behind a rule
  the style doc states tersely.
- **Cross-cutting analysis:** [`docs/code-analysis-2026-05-03.md`](docs/code-analysis-2026-05-03.md)
  enumerates every place in the codebase where the patterns from the
  feedback recur, with sequencing for cleanup.

## Operating rules

- Do not modify project lint rules, formatter configuration, ruff /
  pyrefly settings, or CI step definitions without explicit operator
  instruction.
- Tests must stay green at every step. Run `make test` (unit) before
  committing; run `make test-integration` when changes touch sandbox,
  CLI round-trips, or engine adapters.
- Refactors land behind tests. If coverage is missing for the area
  you are about to refactor, add a characterization-test commit
  first.

## Domain primer

- `litehive/domain/` defines the canonical enums and dataclasses
  (`PipelineState`, `TaskStage`, `TaskStatus`, `PipelineStatus`,
  `PipelineMode`, etc.). Use them; do not pass these as raw strings.
- `litehive/git/ops.py` is the only allowed home for git
  subprocess calls.
- `litehive/state/` is the persistence layer (SQLite). The workspace
  no longer keeps file-based state outside artifacts.
- `litehive/agents/` owns subagent invocation; `litehive/roles/`
  owns prompt policy; `litehive/lifecycle/` owns state-machine
  composition. Cross-package imports must respect those boundaries.
