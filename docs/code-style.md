# Code Style

This file records local style decisions that are easy to regress during refactors.

## Imports

- All imports live at the top of the module. Inline imports inside
  functions/methods are only allowed when (a) the import is genuinely
  heavy and the call site needs cold-start latency or (b) it breaks
  a circular dependency that cannot be untangled by reorganizing
  modules. Each remaining inline import must carry a comment
  explaining the reason (`# inline: …`).
- Do not use `from __future__ import annotations`. The project
  targets a Python where annotations are already evaluated lazily by
  default, and the import only complicates runtime introspection
  (e.g. `dataclasses.fields(...).type` becoming a string).
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

## Domain Values

- Do not store, compare, or pass-around domain values (stages,
  pipeline states, verdicts, modes, roles, statuses) as raw
  strings. Use the enums in `litehive/domain/common.py`
  (`PipelineState`, `TaskStage`, `PipelineStatus`, `PipelineMode`,
  …). String comparisons against domain values rot silently when
  enums are renamed; typed values fail loudly.
- Convert at the boundary. Strings entering the system from the
  database, JSON payloads, or CLI arguments should be converted
  immediately via `canonical_pipeline_state(...)` (or its
  equivalent) and then carried as the typed value.
- Do not paper over the impedance with `# type: ignore[arg-type]`.
  If you find yourself reaching for one, fix the receiver instead.

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
