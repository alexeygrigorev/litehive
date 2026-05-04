.PHONY: test test-unit test-integration test-all lint typecheck

# Default: fast unit tests only (tests/).
test: test-unit

test-unit:
	uv run pytest tests/

# Opt-in: integration suite under tests_integration/ — real binaries,
# real bwrap sandboxes, real CLI round-trips. Slow and may require
# engine-specific binaries (codex, claude, etc.); individual tests skip
# when their dependencies are missing.
test-integration:
	uv run pytest tests_integration/

# Both suites, one invocation.
test-all:
	uv run pytest tests/ tests_integration/

lint:
	uv run ruff check litehive tests tests_integration
	uv run ruff format --check litehive tests tests_integration

# Type-check against the recorded baseline. CI should run this; only
# new type errors (above the baseline) will fail. To refresh the
# baseline after intentional widening, run:
#   uv run pyrefly check --baseline pyrefly-baseline.json --update-baseline
typecheck:
	uv run pyrefly check --baseline pyrefly-baseline.json
