"""Verify workspace runner_hooks config is translated into HookSpec lists."""

from types import SimpleNamespace

from litehive.domain.common import PipelineState
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

    assert set(out.keys()) == {PipelineState.BEFORE_GROOMING, PipelineState.AFTER_IMPLEMENTING}
    assert isinstance(out[PipelineState.BEFORE_GROOMING][0], HookSpec)
    assert out[PipelineState.BEFORE_GROOMING][0].command == "echo pre-groom"
    assert out[PipelineState.BEFORE_GROOMING][0].timeout_seconds == 30
    assert out[PipelineState.BEFORE_GROOMING][0].description is None
    assert out[PipelineState.AFTER_IMPLEMENTING][0].command == "pytest -q"
    assert out[PipelineState.AFTER_IMPLEMENTING][0].timeout_seconds == 120
    assert out[PipelineState.AFTER_IMPLEMENTING][0].description == "full suite"
    assert out[PipelineState.AFTER_IMPLEMENTING][0].instructions_on_failure == "fix tests"


def test_hook_specs_from_config_supports_string_commands() -> None:
    config = SimpleNamespace(runner_hooks={"after_implementing": ["uv run ruff check ."]})

    out = hook_specs_from_config(config)

    assert out[PipelineState.AFTER_IMPLEMENTING][0] == HookSpec(command="uv run ruff check .")


def test_hook_specs_from_config_skips_empty_phases_and_missing_attr() -> None:
    assert hook_specs_from_config(SimpleNamespace()) == {}

    config = SimpleNamespace(runner_hooks={"before_grooming": [], "after_testing": None})
    assert hook_specs_from_config(config) == {}
