# Code Style

This file records local style decisions that are easy to regress during refactors.

## Imports

- All imports live at the top of the module. Inline imports inside
  functions/methods are only allowed when (a) the import is genuinely
  heavy and the call site needs cold-start latency or (b) it breaks
  a circular dependency that cannot be untangled by reorganizing
  modules. Each remaining inline import must carry a comment
  explaining the reason (`# inline: …`).
- "Only used in this one function" is **not** a valid reason to
  keep an import inline. Hoist it. Inline imports are not a
  code-organization tool.
- Do not alias modules with `from X import Y as Z`. If you need
  a module reference (e.g. for monkey-patching in tests), use
  `import X.Y` and refer to symbols by the full dotted path.
  Aliasing hides the real module name and invites brittle
  test-side patching.
- Do not use `from __future__ import annotations`. The project
  targets a Python where annotations are already evaluated lazily by
  default, and the import only complicates runtime introspection
  (e.g. `dataclasses.fields(...).type` becoming a string).
- `if TYPE_CHECKING:` blocks need the same justification as inline
  imports. If the same module is also imported at runtime
  elsewhere in the file, the TYPE_CHECKING block buys nothing —
  drop it. Only keep one when it actually breaks a runtime cycle
  while preserving the type annotation.
- Import directly from the module that owns the behavior.
- Do not add thin wrapper modules that only re-export imports from another file.
- Do not add modules whose whole body is `from x import ...` plus `__all__`.
- Do not maintain giant `__all__` export bags in internal helper modules.
- If a module is only imported explicitly by name, `__all__` adds maintenance cost and no value.
- If a helper module exists, it should contain real helper behavior, fixtures, or test utilities, not just forwarding imports.

Bad:

```python
from ._shared import (
    Foo,
    Bar,
)

__all__ = ["Foo", "Bar"]
```

Good:

```python
from package.feature import Foo, Bar
```

Good when the module owns real helper code:

```python
from tests.support.helpers import make_workspace, run_cli
```

## Compatibility

- Prefer current-shape code over compatibility branches.
- Do not keep backward-compatibility shims for removed config keys, file layouts, or module paths unless there is an active migration plan that still requires them.
- When a legacy path is intentionally removed, delete the fallback instead of translating it at runtime.
- Do not add dedicated `Legacy...Error` exception types or dead-format branches just to recognize removed shapes.
- If the current contract already fails through normal schema validation or a current-state error, use that path and delete the compatibility-specific handling.
- Keep loaders strict. Unknown or removed config keys should fail normally instead of being warned about and ignored.
- Avoid “just in case” support for historical formats when the product only supports the current format.
- Do not keep one-off tests whose only purpose is to assert that a removed legacy shape still errors.
- Do not keep one-off tests whose only purpose is to assert that a removed legacy shape is silently ignored.
- Once old compatibility code is deleted, prefer covering the current contract rather than freezing a dedicated rejection test for the dead format.
- Do not add tests for removed CLI flags, removed config keys, or removed task fields when the only thing being asserted is “this old shape is still rejected”.
- Once a feature is removed, delete its compatibility path and move on instead of keeping dedicated compatibility tests for it.
- Do not silently rewrite invalid current-shape config to a default.
- Do not keep loaders “helpful” by swallowing bad current config and returning an empty/default config object.
- Do not fall back to a generic/default mode when the caller already provided a current required key and it is invalid.

## State And Ownership

- Keep one source of truth for runtime state.
- Do not mirror the same execution state across multiple models and then keep them in sync with bridge code.
- If a state machine owns task execution state, read that state directly instead of copying it into another task/runtime layer.
- Do not split one logical transition across multiple modules unless the split removes real complexity.
- Prefer one owning module per concern:
  - one owner for task transitions
  - one owner for worktree recovery
  - one owner for lockfile metadata
  - one owner for status snapshot construction
- Do not implement the same transition twice, once as a dedicated function and again as a generic update branch.
- Do not keep duplicate aliases, wrapper assignments, or second public entry points that do the same state mutation.

## Abstractions

- Do not introduce list-based plugin/probe/repair abstractions when production uses exactly one implementation.
- If there is only one real probe, repair, backend, or policy, code it directly.
- Prefer deleting speculative extensibility over preserving it “just in case”.
- If a class exists only to generalize one current call site, collapse it unless there is a committed near-term second implementation.
- Keep CLI code thin. Business logic should not be reimplemented in CLI handlers.
- Prefer composition and delegation over inheritance. Mixins are not
  used in this codebase; a helper that would otherwise be a mixin
  should become a named collaborator injected into the class that
  needs it.
- Do not keep free functions that only call one other function and
  add no domain behavior. Delete the wrapper, move the real behavior
  to the caller, or make it a method on the domain object that owns
  the behavior.
- Treat returned tuples from business logic as a design smell. If the
  values have domain meaning, return a named dataclass or domain
  object so readers can tell what each value means without unpacking
  positionally.
- Do not pass plain `object` values or untyped sentinels through
  business logic. If absence is a real state, model it explicitly; if
  the value is always present, make the type concrete and required.
- Avoid `getattr` on internal domain/config/runtime objects. It hides
  the real contract from static analysis and readers. If dynamic
  attribute access is genuinely required at an adapter boundary, keep
  it isolated and explain why.

## Control Flow

- Prefer flat early-return over nested `if x is not None:` (or any
  guard) blocks. If the negative branch is short, write
  `if x is None: return <something>` and continue at the top level
  instead of indenting the rest of the function under the positive
  branch.
- Required parameters should be required. Do not give a parameter
  like `root: Path | None = None` a default just because some
  call site doesn't have one — make the missing data the call
  site's problem.
- Do not write ternary expressions (`a if cond else b`). Use a
  full `if`/`else` block. The exception is when the ternary is the
  only readable form inside a larger expression that genuinely must
  stay one line (rare). For nullable defaults, prefer `value or
  fallback` over `value if value is not None else fallback`.
- No nested ternaries. They are hard to read and rewrite-hostile.
- No bare `*` keyword-only markers in function signatures. Write
  `def func(a, b, c)` instead of `def func(a, b, *, c)`. Callers
  can still pass parameters by keyword. Splitting positional from
  keyword-only adds noise without protecting anything in this
  codebase.
- Review every `isinstance` in production code. Most checks mean the
  caller or loader failed to return a useful domain type. Remove the
  check by returning a typed object; when an `isinstance` remains at a
  real boundary, add a short comment explaining why runtime narrowing
  is needed there.
- Do not use repeated `None` defaults and guards to paper over states
  that should be impossible. Optional fields are for real domain
  states, not for avoiding a proper constructor or state transition.

## Hoist inline expressions to named locals

- Inline string operations like `latest.message.splitlines()[0]`,
  expression chains, and one-line transformations get hoisted to
  a named local variable when (a) the result is used more than
  once, or (b) the inline form is hard to read at a glance.
  Names document intent; chains do not.
- Same for diagnostic dicts and other "what is this thing?"
  expressions: `failure_diagnostics = {...}` then pass
  `failure_diagnostics`. The reader should be able to tell from
  the name what the value represents without parsing the
  construction expression.

## Domain Values

- Do not store, compare, or pass-around domain values (stages,
  pipeline states, verdicts, modes, roles, statuses, outcomes) as raw
  strings. Use the owning domain enum, such as `PipelineState` from
  `litehive/domain/common.py` or `TaskOutcomeKind` from
  `litehive/domain/outcomes.py`. String comparisons against domain
  values rot silently when enums are renamed; typed values fail loudly.
- Convert at the boundary. Strings entering the system from the
  database, JSON payloads, or CLI arguments should be converted
  immediately via `canonical_pipeline_state(...)` (or its
  equivalent) and then carried as the typed value.
- Do not paper over the impedance with `# type: ignore[arg-type]`.
  If you find yourself reaching for one, fix the receiver instead.
- Domain relationships belong on domain objects, not in scattered
  dictionaries. If a role has a default stage, the role object should
  expose that fact; if a stage has an owner role, the stage object
  should expose that fact.
- Avoid broad dictionaries for internal domain events, reports,
  sessions, diagnostics, runtime settings, and command results. Use a
  dataclass or domain model with fields that explain the contract.
- Config/profile data that starts as YAML or JSON should be validated
  into typed models at the boundary. Do not spread mapping-shape
  checks throughout business logic when one model loader can own the
  conversion.

## Module Organization

- **Findability** is the criterion. When deciding which file or
  package something lives in, pick the placement that lets the
  next reader locate it without project-history knowledge. If a
  domain dataclass is hidden inside a 1400-line utility module,
  that's a findability bug.
- A function that lives in module A but whose only callers are
  in module B should move to module B.
- Names that hint at history ("fast_status", "v2_handler",
  "legacy_*") cost everyone who comes later — rename them.

## Classes vs. Free Functions

- A class whose methods only delegate to free functions is not
  a class — it's a namespace with extra steps. Either own the
  behavior in methods, or get rid of the class.
- When a class has grown too big, split its responsibilities
  into smaller classes that *interact with each other*. Do not
  keep the outer class as a façade in front of free functions.
- A function that takes `workspace` as its first argument is a prompt
  to ask whether it should be a `Workspace` method or a method on a
  focused service owned by the workspace.
- Functions with four or more parameters need review. When the
  values travel together as a concept, introduce a named domain
  object instead of lengthening the signature.
- Functions longer than one screen need review. Use 25 lines as the
  practical threshold; split longer functions into focused helpers
  whose names describe the domain step they perform.
- Single Responsibility Principle applies to service classes. If the
  reader cannot summarize what the class owns in one sentence, split
  the collaborators before adding more behavior.

## Dependency Injection

- **Do not initialize collaborators inside `__init__`.** A
  constructor that calls `Workspace.from_path(root)`,
  `load_config(root)`, `SandboxLauncher(root, config)`, or any
  other factory to build the things it depends on is hiding
  wiring inside the class. That makes the class hard to test
  (every test has to satisfy every transitive dependency the
  constructor reaches for) and turns every refactor of a
  collaborator into a hunt across `__init__` bodies.
- Constructors take their dependencies as parameters. The class
  trusts them; it does not validate, build, or replace them.
- One module owns the wiring. There is one container (a
  factory module or a small typed dataclass) that knows how to
  build the long-lived graph from a workspace path and exposes
  the ready-to-use objects. Tests build the container with fakes;
  production builds it with real adapters.
- One initialization point. The CLI / process entry builds the
  container once at startup and threads its handles into every
  command handler. After that point no code calls
  `Workspace.from_path` or `load_config` — they read the cached
  values out of the container.
- Raw `root: Path` belongs only at the outermost boundary where
  operator input is resolved. The target shape is one conversion
  point: `Path` → DI container. Internal functions receive the
  container, a `Workspace`, or a narrower service/repository from
  that container, never a raw root they can re-interpret.
- Service objects follow the same rule as other classes:
  constructors accept ready dependencies and store them. They do
  not call factories, load config, open databases, or construct
  collaborators. The DI container is the only production code that
  performs that assembly.
- Environment reads (`os.environ.get`, `os.environ.copy`, and
  similar) belong at DI/config/process boundaries. Business logic and
  CLI command bodies receive resolved settings as parameters or
  services, not by reading process environment directly.

This is an incremental migration: the codebase still has many
``__init__`` bodies that do their own wiring (SubagentManager,
RuntimeStore, HeruEngineAdapter, etc.). When you touch one of
those classes for any other reason, take the opportunity to
hoist the wiring out of `__init__` into the container.

## Package `__init__.py`

- `__init__.py` files hold no behavior — no typer apps, no CLI
  registration, no side-effecting imports. Re-exporting from
  submodules is also discouraged, because it forces every caller
  of one submodule to load every other (hidden import cycles).
- They *should* hold a docstring naming each module in the
  package and what it owns, so a reader can navigate the
  package without `grep -r`.

## Agent Authority Over Project Configuration

- Lint rules, formatter configuration, CI step definitions, and
  ruff/pyrefly settings are operator-owned. Subagents do not
  modify them without explicit operator instruction.
- If a project rule disappears mysteriously and an agent ran
  recently, suspect the agent first.

## Workspace Identity

- `root: Path | None` is a code smell inside business logic. The
  workspace is not optional — there is no meaningful "no
  workspace" state below the CLI entry point. Drop the default,
  drop the optionality, drop the `if root is not None:` guard.
  The only legitimate identity check belongs at the CLI / process
  entry where a `Path` is first turned into a workspace handle.
- Helpers should ask for the *thing they actually need* —
  ideally a `Workspace` (or similar) value object that owns the
  SQLite store, config, and paths — not a raw `Path`. The
  current pattern of threading `root: Path` through nearly every
  function is a holdover from the file-based era; SQLite is now
  the source of truth and the workspace identity should be
  explicit.
- During the remaining migration, treat any `root: Path`
  parameter below the CLI/process boundary as temporary debt. Do
  not add new internal `root` parameters. If a helper needs
  workspace identity, pass `Workspace`; if it needs several
  collaborators, pass the container or a focused service assembled
  by the container.

## Refactoring Discipline

- Before any non-trivial refactor (file split, module move,
  storage swap, helper deletion, enum reshape), the affected
  behavior must be covered by tests **first**. The refactor
  series begins with a "characterization tests" commit if
  coverage is missing; the structural commits come after, with
  the suite green at each step.
- The bar for new tests in this position is "the test would
  have failed before the fix". A test that asserts the new shape
  but never failed under the old shape does not protect
  anything and can be deleted.

## Function Docstrings

- Non-trivial helper docstrings use the multi-line form: opening
  triple quotes on their own line, body on following lines, and
  closing triple quotes on their own line. One-line docstrings are
  only for genuinely tiny, obvious helpers.
- Helpers whose existence is not obvious from the name need a
  docstring saying what problem they solve and why they exist. The
  "why" is the part that rots silently when the surrounding code
  changes; without it, future readers can't tell whether the helper
  is still load-bearing.
- When the caller is not obvious — different module, more than
  one, or only one but in an unexpected place — name the caller
  in **domain terms**: "called by the accepting stage when the
  reviewer submits a reject", not "called from
  `runner.py:387`". Code paths get renamed; the domain story
  stays true. File/function references in docstrings become
  silent lies the moment something moves.
- For helpers with exactly one obvious caller in the same module,
  the location is self-documenting; don't add a callers note for
  its own sake.
- Do not write docstrings that merely restate the function name.
  `write_stream_artifact` does not need "writes stream artifact"; it
  needs to say which actor writes it, why that artifact exists, and
  how later code uses it.
- Document parameters whose domain meaning is not obvious from the
  type and name. If a parameter is called `source`, `reason`,
  `payload`, `context`, or `data`, the docstring usually needs to
  explain what values are valid and who supplies them.
- Keep docstrings readable in plain code. Avoid Markdown-heavy
  formatting such as `**bold**` and double-backtick markup; use plain
  text and single backticks only when naming code symbols helps.
- Wrap docstrings and prose around 80 characters where practical.
  Long prose inside compact code blocks is harder to review than a
  few short lines.

## Subagent Artifacts

- Do not delete subagent execution evidence (prompts, stdout/stderr
  streams, execution traces) on the success path. They are valuable
  for retrospective debugging when something fails downstream.
- Format-flip cleanup (e.g. removing the `.gz` variant when writing
  the plain variant of the same artifact) is fine and should be
  documented as such in the helper.

## Side-Effecting Subsystems

- One module per side-effecting subsystem. All git invocations go
  through `litehive/git/ops.py`. No raw `subprocess.run(["git", …])`
  outside that module. If a needed helper is missing, add it there.
- Helpers always take an explicit `cwd: Path`. Do not `os.chdir`.

## Prompts

- Long prompt strings do not live as Python literals. Prompts go
  in templates (Jinja2 under `templates/prompts/`) and are rendered
  by a typed builder. The Python side passes typed inputs.
- Do not add free-form `dict[str, str]` slots to prompt builders.
  Each input should be a typed field with a docstring explaining
  what the agent uses it for.
- Domain prose (e.g. "stage owner for grooming") belongs on the
  domain object as a property/method, not as ad-hoc text in the
  prompt module.

## Defensive Coding

- Do not auto-heal broken state in hot-path selection code.
- Repair corrupted queue/runtime state explicitly in repair flows, not opportunistically during normal execution.
- Avoid broad `except Exception` handlers on mainline paths.
- If a failure means the current state is broken, fail loudly with a clear error instead of silently returning an empty/default value.
- Do not use repeated `None`/missing fallbacks to paper over states that should be impossible under the current contract.
- Avoid defensive `.get(..., default)` or `or {}` / `or []` chains on strongly typed internal objects.
- Do not duplicate parsing logic for the same file format in multiple places.
- Do not re-implement PID/liveness/lockfile checks in multiple modules.

## Duplication

- Do not keep two independent implementations of the same command surface.
- Prefer one status path with shared formatting helpers over separate “fast”, “full”, and ad hoc status builders.
- Do not duplicate policy logic in both generated inline scripts and checked-in Python modules.
- If code must generate a helper script, it should reuse a shared implementation or template instead of embedding a second copy of the logic in a string literal.
- Do not duplicate backend-specific logic when one supported backend is enough for the product.
- Avoid supporting multiple selection policies or execution modes unless users actively rely on them and the added complexity is justified.

## Test code

- Import production functions, classes, and constants directly.
- Keep test-support imports limited to actual helpers defined in the test suite.
- Prefer fewer layers of indirection over “organized” wrapper modules.
- Do not write tests whose only assertion is that a module imports, an alias resolves, or a symbol still exists at an old path unless the import path itself is the product contract.
- Do not rely on leading shell env assignments in routine examples or workflows when the setting can live in code, fixtures, or explicit config.
- Prefer checked-in helper behavior over commands like `FOO=bar uv run ...` that operators can forget.

## Avoid low-value template tests

- Do not write tests that only assert static scaffold text, headings, or prompt copy.
- Avoid tests like `test_render_context_template_shows_base_and_project_stage_scaffolding` that mostly check for hard-coded strings such as section names or prose snippets.
- These tests are low value because they:
  - fail on harmless wording edits
  - duplicate the template contents instead of validating behavior
  - make refactors noisy without protecting a real contract
  - encourage frozen prompt copy when the implementation should stay easy to edit
- Prefer testing actual contracts instead:
  - returned profile names and keys
  - merge behavior between shared and overlay config
  - required sections only when another parser or feature depends on them
  - rendering behavior that affects control flow, not editorial wording
- Prefer one test per unique behavior path.
- Avoid a second heavy end-to-end test whose only extra assertion is “artifact file exists” when another test already covers creation/update of the same artifact.
- Do not write tests that only assert `--help` copy or operator-facing explanatory text.
- For CLI help, test command registration, accepted/rejected arguments, and behavioral wiring. Do not freeze narrative wording.
- More generally: do not write tests that only assert output text when no logic, state, branching, or data transformation is being exercised.
- If the output is static and would change only because wording changed, it is documentation churn, not a behavioral regression.
- Avoid tests that only assert framework-surface errors such as Typer-generated `No such command` messages.
- Avoid tests that only prove a tiny test-only wrapper printed the expected text when the underlying production behavior is already covered elsewhere.
