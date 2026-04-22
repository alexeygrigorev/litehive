from pathlib import Path

import pytest
import yaml

from litehive.config.loading import load_config
from litehive.config.workspace import ensure_workspace


def test_load_config_normalizes_runner_hooks_and_ignores_legacy_flags(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner_hooks": {
                    "before_implementing": ["echo pre"],
                    "after_implementing": [
                        {"command": "echo post", "reject_on_failure": True},
                        {"command": "uv run pytest -q", "timeout_seconds": 300, "description": "full suite"},
                    ],
                    "after_commit": [{"command": "echo verify", "blocking": True}],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.runner_hooks == {
        "before_implementing": [{"command": "echo pre"}],
        "after_implementing": [
            {"command": "echo post"},
            {"command": "uv run pytest -q", "timeout_seconds": 300.0, "description": "full suite"},
        ],
        "after_commit": [{"command": "echo verify"}],
    }


def test_load_config_preserves_runner_hook_descriptions_and_instructions(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner_hooks": {
                    "after_implementing": [
                        {
                            "command": "uv run ruff check .",
                            "description": "ensures lint passes before acceptance",
                            "instructions_on_failure": "fix lint first",
                        }
                    ]
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.runner_hooks["after_implementing"][0]["description"] == "ensures lint passes before acceptance"
    assert config.runner_hooks["after_implementing"][0]["instructions_on_failure"] == "fix lint first"


def test_configure_rejects_removed_runner_hook_points(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {"runner_hooks": {"after_merge": [{"command": "echo nope"}]}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runner_hooks key must be one of:"):
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
