from pathlib import Path

import yaml

from litehive.config.engine_models import resolve_engine_name
from litehive.config.loading import load_config_for_workspace
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import create_workspace
from litehive.state.records import create_task_for_workspace
from litehive.workspace import Workspace


def _load_config(root: Path) -> LitehiveConfig:
    return load_config_for_workspace(Workspace.from_path(root))


def test_resolve_engine_name_allows_claude_task_when_workspace_defaults_to_claude(
    tmp_path: Path,
) -> None:
    create_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Claude task")
    config = _load_config(tmp_path)

    assert resolve_engine_name(task, config) == "claude"


def test_resolve_engine_name_allows_workspace_default_claude(tmp_path: Path) -> None:
    create_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    workspace = Workspace.from_path(tmp_path)
    config = _load_config(tmp_path)

    task = create_task_for_workspace(workspace, title="Claude default task")
    assert config.default_engine == "claude"
    assert resolve_engine_name(task, config) == "claude"


def test_claude_is_not_default_engine() -> None:
    config = LitehiveConfig()
    assert config.default_engine != "claude"


def test_claude_config_defaults_to_sonnet() -> None:
    config = LitehiveConfig()
    assert config.claude_model == "claude-sonnet-4-20250514"
    assert config.claude_max_turns == 100


def test_claude_not_in_engine_preference() -> None:
    config = LitehiveConfig()
    assert "claude" not in config.engine_preference


def test_configure_persists_claude_settings(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    raw = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw["claude_model"] = "claude-sonnet-4-20250514"
    raw["claude_max_turns"] = 20
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    config = _load_config(tmp_path)
    assert config.claude_model == "claude-sonnet-4-20250514"
    assert config.claude_max_turns == 20


def test_configure_updates_existing_workspace_process_profile(tmp_path: Path) -> None:
    create_workspace(tmp_path, LitehiveConfig(process_profile="generic"))
    raw = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw["process_profile"] = "python"
    raw["claude_max_turns"] = 20
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )

    config = _load_config(tmp_path)
    assert config.process_profile == "python"
    assert config.claude_max_turns == 20


def test_claude_model_resolved_from_workspace_defaults() -> None:
    config = LitehiveConfig(claude_model="claude-sonnet-4-20250514")
    assert config.model_for_engine("claude") == "claude-sonnet-4-20250514"

    config_default = LitehiveConfig()
    assert config_default.model_for_engine("claude") == "claude-sonnet-4-20250514"
