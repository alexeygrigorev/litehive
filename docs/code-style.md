# Code Style

This file records local style decisions that are easy to regress during refactors.

## Imports

- Import directly from the module that owns the behavior.
- Do not add thin wrapper modules that only re-export imports from another file.
- Do not add modules whose whole body is `from x import ...` plus `__all__`.
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
- Keep loaders strict. Unknown or removed config keys should fail normally instead of being warned about and ignored.
- Avoid “just in case” support for historical formats when the product only supports the current format.

## Test code

- Import production functions, classes, and constants directly.
- Keep test-support imports limited to actual helpers defined in the test suite.
- Prefer fewer layers of indirection over “organized” wrapper modules.

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
