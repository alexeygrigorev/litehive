from pathlib import Path

from litehive.config.runtime_settings import (
    RuntimeSettingKey,
    RuntimeSettingsRepository,
)
from litehive.config.workspace import create_workspace
from litehive.workspace import Workspace


def test_runtime_settings_bootstrap_from_config_data_once(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    repository = RuntimeSettingsRepository(workspace)

    repository.bootstrap(
        {
            "default_engine": "gemini",
            "engine_preference": ["gemini", "codex"],
            "engine_freeze": {"codex": "2099-01-01T00:00:00Z"},
        },
    )

    assert repository.load() == {
        "default_engine": "gemini",
        "engine_preference": ["gemini", "codex"],
        "engine_freeze": {"codex": "2099-01-01T00:00:00Z"},
    }

    repository.bootstrap(
        {
            "default_engine": "codex",
            "engine_preference": ["codex"],
            "engine_freeze": {},
        },
    )

    assert repository.load() == {
        "default_engine": "gemini",
        "engine_preference": ["gemini", "codex"],
        "engine_freeze": {"codex": "2099-01-01T00:00:00Z"},
    }


def test_runtime_settings_apply_database_values_over_config_data(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    repository = RuntimeSettingsRepository(workspace)
    repository.bootstrap(
        {
            "default_engine": "gemini",
            "engine_preference": ["gemini", "codex"],
            "engine_freeze": {},
        },
    )

    effective = repository.apply_to_config_data(
        {
            "default_engine": "codex",
            "engine_preference": ["codex"],
            "engine_freeze": {"opencode": "2099-01-01T00:00:00Z"},
            "pool_max_tasks": 7,
        },
    )

    assert effective["default_engine"] == "gemini"
    assert effective["engine_preference"] == ["gemini", "codex"]
    assert effective["engine_freeze"] == {}
    assert effective["pool_max_tasks"] == 7


def test_runtime_settings_repository_writes_audit_entry_and_noops_when_unchanged(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    repository = RuntimeSettingsRepository(workspace)

    changed = repository.set(
        key=RuntimeSettingKey.DEFAULT_ENGINE,
        value="gemini",
        actor="operator",
        source="test",
        context={"reason": "coverage"},
    )
    unchanged = repository.set(
        key=RuntimeSettingKey.DEFAULT_ENGINE,
        value="gemini",
        actor="operator",
        source="test",
        context={"reason": "coverage"},
    )

    assert changed.changed is True
    assert unchanged.changed is False
    assert unchanged.old_value == "gemini"
    entries = repository.audit_entries(key="default_engine", limit=5)
    assert len(entries) == 1
    assert entries[0].actor == "operator"
    assert entries[0].source == "test"
    assert entries[0].old_value == "codex"
    assert entries[0].new_value == "gemini"
    assert entries[0].context == {"reason": "coverage"}


def test_runtime_settings_tolerate_malformed_stored_json(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    repository = RuntimeSettingsRepository(workspace)
    repository.bootstrap()

    with workspace.connect() as connection:
        connection.execute(
            "UPDATE runtime_settings SET value_json = ? WHERE key = ?",
            ("{", "default_engine"),
        )
        connection.execute(
            """
            INSERT INTO runtime_settings_audit_log (
                key,
                created_at,
                actor,
                source,
                old_value_json,
                new_value_json,
                context_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "default_engine",
                "2026-05-08T00:00:00Z",
                "operator",
                "test",
                "{",
                "{",
                "{",
            ),
        )

    assert repository.load()["default_engine"] is None
    entries = repository.audit_entries(key="default_engine", limit=1)
    assert entries[0].old_value is None
    assert entries[0].new_value is None
    assert entries[0].context == {}
