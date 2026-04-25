from pathlib import Path

from litehive.config import (
    LitehiveConfig,
    config_path,
    ensure_workspace,
    load_config,
    load_context,
    litehive_root,
    merge_config_layers,
    normalize_workspace_root,
    render_workspace_gitignore,
    resolve_workspace,
    workspace_data_dir,
    workspace_path,
)


def test_config_facade_reexports_public_api(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    workspace = tmp_path / "workspace"

    ensure_workspace(workspace)

    assert LitehiveConfig().default_engine == "codex"
    assert config_path(workspace) == workspace / ".litehive" / "config.yaml"
    assert load_config(workspace).default_engine == "codex"
    assert load_context(workspace)
    assert litehive_root() == tmp_path / "xdg-data" / "litehive"
    assert merge_config_layers({"a": {"b": 1}}, {"a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}
    assert normalize_workspace_root(workspace, source="test") == workspace.resolve()
    assert render_workspace_gitignore().startswith(".lock")
    assert resolve_workspace(None, cwd=workspace, register=False) == workspace.resolve()
    assert workspace_data_dir(workspace) == workspace_path(workspace)
