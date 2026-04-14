# Code Style

This file records local style decisions that are easy to regress during refactors.

## Imports

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
- Once a feature is removed, delete its compatibility path and move on instead of keeping dedicated tombstone tests for it.
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
