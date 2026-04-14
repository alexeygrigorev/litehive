# Integration test suite

`tests_integration/` is the slower integration suite for Litehive. It is intentionally separate from `tests/` so default unit runs stay fast and deterministic.

Engine matrix derived from `litehive.config.VALID_ENGINE_NAMES`:

- `codex`
- `opencode`
- `gemini`
- `copilot`
- `claude`
- `goz`

All engine tests are opt-in because they require locally installed, authenticated CLIs and can consume quota.

Run the full suite:

```bash
LITEHIVE_INTEGRATION_ENGINES=codex,opencode,gemini,copilot,claude,goz uv run pytest tests_integration/ -q
```

Run a single engine plus the non-engine CLI integration coverage:

```bash
LITEHIVE_INTEGRATION_ENGINES=codex uv run pytest tests_integration/ -q
```

Run one engine file:

```bash
LITEHIVE_INTEGRATION_ENGINES=gemini uv run pytest tests_integration/test_gemini.py -q
```

Optional timeout override:

```bash
LITEHIVE_INTEGRATION_TIMEOUT_SECONDS=30 uv run pytest tests_integration/ -q
```

Skip rules:

- If `LITEHIVE_INTEGRATION_ENGINES` is unset, real engine tests are skipped.
- If an engine is not named in `LITEHIVE_INTEGRATION_ENGINES`, that engine file is skipped.
- If the requested engine binary is not on `PATH`, that engine file is skipped.
- If a requested engine is installed but not authenticated, the test fails so the operator sees the real adapter error instead of a silent omission.
