"""Verify workspace runner_hooks config is translated into HookSpec lists."""

from types import SimpleNamespace

from litehive.lifecycle.nodes.hook import HookSpec
from litehive.lifecycle.orchestration import hook_specs_from_config


def test_hook_specs_from_config_copies_known_fields() -> None:
    config = SimpleNamespace(
        runner_hooks={
            "before_grooming": [{"command": "echo pre-groom", "timeout_seconds": 30}],
            "after_implementing": [
                {
                    "command": "pytest -q",
                    "timeout_seconds": 120,
                    "description": "full suite",
                    "instructions_on_failure": "fix tests",
                }
            ],
        }
    )

    out = hook_specs_from_config(config)

    assert set(out.keys()) == {"before_grooming", "after_implementing"}
    assert isinstance(out["before_grooming"][0], HookSpec)
    assert out["before_grooming"][0].command == "echo pre-groom"
    assert out["before_grooming"][0].timeout_seconds == 30
    assert out["before_grooming"][0].description is None
    assert out["after_implementing"][0].command == "pytest -q"
    assert out["after_implementing"][0].timeout_seconds == 120
    assert out["after_implementing"][0].description == "full suite"
    assert out["after_implementing"][0].instructions_on_failure == "fix tests"


def test_hook_specs_from_config_supports_string_commands() -> None:
    config = SimpleNamespace(runner_hooks={"after_implementing": ["uv run ruff check ."]})

    out = hook_specs_from_config(config)

    assert out["after_implementing"][0] == HookSpec(command="uv run ruff check .")


def test_hook_specs_from_config_skips_empty_phases_and_missing_attr() -> None:
    assert hook_specs_from_config(SimpleNamespace()) == {}

    config = SimpleNamespace(runner_hooks={"before_grooming": [], "after_testing": None})
    assert hook_specs_from_config(config) == {}
