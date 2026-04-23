This directory stores the local `heru` wheel used by repo-root `uv` commands.

The root `pyproject.toml` pins `[tool.uv.sources].heru` to the wheel in this
directory so `uv sync`, `uv run`, and `uv pip install heru` resolve the checked-in
package instead of the unrelated `heru` package published on PyPI.

Regenerate after changing the standalone package contents or metadata:

```bash
uv build --package heru --wheel -o packages --clear --no-create-gitignore
```
