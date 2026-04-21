from pathlib import Path

import pytest
import yaml

from litehive.config.loading import load_config
from litehive.config.model import (
    DEFAULT_SUBAGENT_INACTIVITY_TIMEOUT_SECONDS,
    LitehiveConfig,
)
from litehive.config.paths import litehive_root
from litehive.config.workspace import ensure_workspace


def test_configure_persists_gemini_model(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["default_engine"] = "gemini"
    raw_config["gemini_model"] = "gemini-2.5-pro"
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.default_engine == "gemini"
    assert config.gemini_model == "gemini-2.5-pro"


def test_configure_persists_copilot_model(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["default_engine"] = "copilot"
    raw_config["copilot_model"] = "gpt-5"
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.default_engine == "copilot"
    assert config.copilot_model == "gpt-5"


def test_configure_persists_process_profile(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["process_profile"] = "rust"
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.process_profile == "rust"


def test_load_config_uses_global_defaults_when_workspace_config_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    global_path = litehive_root() / "config.yaml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        yaml.safe_dump(
            {
                "default_engine": "gemini",
                "pool_stop_on_failure": True,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    (workspace / ".litehive" / "config.yaml").write_text("{}", encoding="utf-8")

    config = load_config(workspace)

    assert config.default_engine == "gemini"
    assert config.pool_stop_on_failure is True


def test_load_config_applies_workspace_overrides_on_top_of_global_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    global_path = litehive_root() / "config.yaml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        yaml.safe_dump(
            {
                "default_engine": "gemini",
                "pool_max_tasks": 7,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    (workspace / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "default_engine": "codex",
                "pool_max_tasks": 2,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(workspace)

    assert config.default_engine == "codex"
    assert config.pool_max_tasks == 2


def test_load_config_deep_merges_global_and_workspace_mappings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    global_path = litehive_root() / "config.yaml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        yaml.safe_dump(
            {
                "engine_freeze": {"gemini": "2099-01-01T00:00:00Z"},
                "agent_startup_guidance": {"swe": ["global guidance"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    (workspace / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "engine_freeze": {"codex": "2099-02-02T00:00:00Z"},
                "agent_startup_guidance": {"swe": ["project guidance"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(workspace)

    assert config.engine_freeze == {
        "gemini": "2099-01-01T00:00:00Z",
        "codex": "2099-02-02T00:00:00Z",
    }
    assert config.agent_startup_guidance == {"swe": ["project guidance"]}


def test_litehive_config_normalizes_retry_on() -> None:
    config = LitehiveConfig(retry_on=["timeout", "network", "timeout", "execution_limit"])

    assert config.retry_on == ["timeout", "network", "execution_limit"]


def test_litehive_config_defaults_include_flat_retry_on() -> None:
    config = LitehiveConfig()

    assert config.subagent_inactivity_timeout_seconds == DEFAULT_SUBAGENT_INACTIVITY_TIMEOUT_SECONDS
    assert config.retry_on == ["execution_limit", "timeout"]


def test_load_config_reads_subagent_inactivity_timeout_override(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["subagent_inactivity_timeout_seconds"] = 42
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.subagent_inactivity_timeout_seconds == 42.0


def test_litehive_config_rejects_unknown_retry_on_kind() -> None:
    with pytest.raises(ValueError, match="retry_on must contain only"):
        LitehiveConfig(retry_on=["timeout", "rate_limit"])


@pytest.mark.parametrize("legacy_value", ["echo hi", "", None])
def test_load_config_rejects_legacy_pre_acceptance_command(tmp_path: Path, legacy_value: str | None) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["pre_acceptance_command"] = legacy_value
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="pre_acceptance_command is no longer supported"):
        load_config(tmp_path)


def test_load_config_rejects_legacy_task_engine_routing(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["task_engine_routing"] = {"research": "gemini", "refactor": "codex"}
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="task_engine_routing is no longer supported"):
        load_config(tmp_path)


def test_load_config_rejects_legacy_engine_fallbacks_key(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["engine_fallbacks"] = {
        "codex": ["goz", "copilot"],
        "goz": ["copilot"],
    }
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="engine_fallbacks is no longer supported"):
        load_config(tmp_path)


def test_load_config_rejects_legacy_runner_hook_execution_mode_key(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["runner_hook_execution_mode"] = "async"
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runner_hook_execution_mode is no longer supported"):
        load_config(tmp_path)


def test_load_config_rejects_unknown_process_profile(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["process_profile"] = "unknown_profile"
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown process_profile 'unknown_profile'"):
        load_config(tmp_path)


def test_load_config_rejects_unknown_pool_selection_policy(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["pool_selection_policy"] = "unknown_policy"
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown pool_selection_policy 'unknown_policy'"):
        load_config(tmp_path)


def test_load_config_still_rejects_unknown_keys(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["unsupported_key"] = True
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown config key 'unsupported_key'"):
        load_config(tmp_path)
