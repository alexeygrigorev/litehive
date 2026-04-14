"""Verify workspace runner_hooks config is translated into v2 HookSpec lists."""

from types import SimpleNamespace

from litehive.lifecycle.nodes.hook import HookSpec
from litehive.lifecycle.orchestration import hook_specs_from_config


def _fake_hook(command: str, *, reject: bool = True, timeout: int = 60) -> SimpleNamespace:
    return SimpleNamespace(
        command=command,
        reject_on_failure=reject,
        timeout_seconds=timeout,
        description="",
        instructions_on_failure="",
    )


def test_hook_specs_from_config_copies_known_fields() -> None:
    config = SimpleNamespace(
        runner_hooks={
            "before_grooming": [_fake_hook("echo pre-groom", reject=False, timeout=30)],
            "after_implementing": [_fake_hook("pytest -q", reject=True, timeout=120)],
        }
    )

    out = hook_specs_from_config(config)

    assert set(out.keys()) == {"before_grooming", "after_implementing"}
    assert isinstance(out["before_grooming"][0], HookSpec)
    assert out["before_grooming"][0].command == "echo pre-groom"
    assert out["before_grooming"][0].reject_on_failure is False
    assert out["before_grooming"][0].timeout_seconds == 30
    assert out["before_grooming"][0].description == ""
    assert out["after_implementing"][0].command == "pytest -q"
    assert out["after_implementing"][0].reject_on_failure is True
    assert out["after_implementing"][0].timeout_seconds == 120
    assert out["after_implementing"][0].description == ""


def test_hook_specs_from_config_skips_empty_phases_and_missing_attr() -> None:
    # Missing runner_hooks entirely
    assert hook_specs_from_config(SimpleNamespace()) == {}

    # Empty per-phase lists are dropped (no empty-list pollution in registry)
    config = SimpleNamespace(runner_hooks={"before_grooming": [], "after_testing": None})
    assert hook_specs_from_config(config) == {}
