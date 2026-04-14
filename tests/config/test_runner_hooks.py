from pathlib import Path

import pytest
import yaml

from litehive.config.loading import load_config
from litehive.config.workspace import ensure_workspace


def test_configure_persists_runner_hooks(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner_hooks": {
                    "before_implementing": [{"command": "echo pre"}],
                    "after_implementing": [{"command": "echo post", "reject_on_failure": True}],
                    "before_accepting": [{"command": "echo review"}],
                    "after_accepting": [{"command": "echo accepted"}],
                    "after_commit": [{"command": "echo verify", "reject_on_failure": True}],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)

    assert config.runner_hooks["before_implementing"][0].reject_on_failure is False
    assert config.runner_hooks["after_implementing"][0].command == "echo post"
    assert config.runner_hooks["after_implementing"][0].reject_on_failure is True
    assert config.runner_hook_execution_mode == "run_all"
    assert config.runner_hooks["before_accepting"][0].command == "echo review"
    assert config.runner_hooks["before_accepting"][0].reject_on_failure is False
    assert config.runner_hooks["after_accepting"][0].reject_on_failure is False
    assert config.runner_hooks["after_commit"][0].command == "echo verify"
    assert config.runner_hooks["after_commit"][0].reject_on_failure is True


def test_load_config_preserves_runner_hook_descriptions(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner_hooks": {
                    "after_implementing": [
                        {
                            "command": "uv run ruff check .",
                            "reject_on_failure": True,
                            "description": "ensures lint passes before acceptance",
                        }
                    ]
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.runner_hooks["after_implementing"][0].description == (
        "ensures lint passes before acceptance"
    )


def test_load_config_preserves_runner_hook_execution_mode(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump({"runner_hook_execution_mode": "fail_fast"}, sort_keys=False),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.runner_hook_execution_mode == "fail_fast"


def test_load_config_rejects_invalid_runner_hook_execution_mode(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump({"runner_hook_execution_mode": "sometimes"}, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runner_hook_execution_mode must be one of:"):
        load_config(tmp_path)


def test_configure_rejects_invalid_runner_hook_point(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {"runner_hooks": {"invalid_hook_point": [{"command": "echo nope"}]}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runner_hooks key must be one of:"):
        load_config(tmp_path)

