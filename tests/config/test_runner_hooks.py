from pathlib import Path

import pytest
import yaml

from litehive.config.loading import load_config
from litehive.config.model import VALID_RUNNER_HOOK_ENTRY_KEYS, VALID_RUNNER_HOOK_POINTS
from litehive.config.workspace import ensure_workspace


def test_load_config_normalizes_runner_hooks(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner_hooks": {
                    "before_implementing": ["echo pre"],
                    "after_implementing": [
                        {"command": "uv run pytest -q", "timeout_seconds": 300, "description": "full suite"},
                    ],
                    "after_commit": [{"command": "echo verify"}],
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
            {"command": "uv run pytest -q", "timeout_seconds": 300.0, "description": "full suite"},
        ],
        "after_commit": [{"command": "echo verify"}],
    }


@pytest.mark.parametrize("unsupported_key", ["blocking", "reject_on_failure", "unsupported_key"])
def test_load_config_rejects_unsupported_runner_hook_entry_keys(tmp_path: Path, unsupported_key: str) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner_hooks": {
                    "after_implementing": [
                        {"command": "echo post", unsupported_key: True},
                    ],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=f"contains unsupported keys: {unsupported_key}"):
        load_config(tmp_path)


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


def test_runner_hook_points_keep_all_supported_names() -> None:
    assert VALID_RUNNER_HOOK_POINTS == {
        "before_grooming",
        "after_grooming",
        "before_implementing",
        "after_implementing",
        "before_testing",
        "after_testing",
        "before_accepting",
        "after_accepting",
        "after_commit",
    }


def test_runner_hook_entry_keys_match_flat_supported_contract() -> None:
    assert VALID_RUNNER_HOOK_ENTRY_KEYS == {
        "command",
        "timeout_seconds",
        "description",
        "instructions_on_failure",
    }
