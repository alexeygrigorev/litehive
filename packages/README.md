This directory stores wheel builds for `heru`.

The root `pyproject.toml` now pins `[tool.uv.sources].heru` to the sibling
`../heru` checkout for local development, but wheel builds can still be staged
here when you want a frozen artifact.

Regenerate after changing the standalone package contents or metadata:

```bash
uv build --package heru --wheel -o packages --clear --no-create-gitignore
```
