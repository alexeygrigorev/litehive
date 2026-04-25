"""Audited runtime settings stored in the workspace database."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

import yaml

from litehive.config.model import LitehiveConfig, normalize_engine_sequence
from litehive.config.paths import litehive_root
from litehive.config.workspace_files import config_path
from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow

RUNTIME_SETTING_KEYS = ("default_engine", "engine_preference", "engine_freeze")


@dataclass(frozen=True)
class RuntimeSettingChange:
    key: str
    old_value: Any
    new_value: Any
    changed: bool


@dataclass(frozen=True)
class RuntimeSettingAuditEntry:
    id: int
    key: str
    created_at: str
    actor: str
    source: str
    old_value: Any
    new_value: Any
    context: dict[str, Any]


def _read_config_layer(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return dict(payload)


def _merge_config_layers(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        merged[key] = (
            _merge_config_layers(current, value)
            if isinstance(current, Mapping) and isinstance(value, Mapping)
            else value
        )
    return merged


def _bootstrap_config_data(root: Path) -> dict[str, Any]:
    config = asdict(LitehiveConfig())
    config = _merge_config_layers(config, _read_config_layer(litehive_root() / "config.yaml"))
    return _merge_config_layers(config, _read_config_layer(config_path(root)))


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _json_loads(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _sequence_value(raw_value: Any, *, field_name: str) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, str) or not isinstance(raw_value, Sequence):
        raise ValueError(f"{field_name} must be a sequence of engine names")
    return normalize_engine_sequence([str(item) for item in raw_value], field_name=field_name)


def _freeze_value(raw_value: Any) -> dict[str, str]:
    if raw_value is None:
        return {}
    if not isinstance(raw_value, Mapping):
        raise ValueError("engine_freeze must be a mapping of engine name to UTC timestamp")
    return {str(key): str(value) for key, value in raw_value.items()}


def _runtime_values_from_config(config_data: Mapping[str, Any]) -> dict[str, Any]:
    defaults = LitehiveConfig()
    return {
        "default_engine": str(config_data.get("default_engine", defaults.default_engine)),
        "engine_preference": _sequence_value(
            config_data.get("engine_preference", defaults.engine_preference),
            field_name="engine_preference",
        ),
        "engine_freeze": _freeze_value(config_data.get("engine_freeze", defaults.engine_freeze)),
    }


def _load_setting_rows(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute("SELECT key, value_json FROM runtime_settings").fetchall()
    return {str(row["key"]): _json_loads(str(row["value_json"])) for row in rows}


def bootstrap_runtime_settings(root: Path, config_data: Mapping[str, Any] | None = None) -> None:
    """Seed audited runtime settings from config files once.

    The config file values are bootstrap-only. After the corresponding database
    rows exist, later config-file drift is ignored by runtime config loading.
    """
    with connect_workspace_db(root) as connection:
        existing_keys = {
            str(row["key"])
            for row in connection.execute(
                "SELECT key FROM runtime_settings WHERE key IN (?, ?, ?)",
                RUNTIME_SETTING_KEYS,
            )
        }
        missing_keys = [key for key in RUNTIME_SETTING_KEYS if key not in existing_keys]
        if not missing_keys:
            return
        runtime_values = _runtime_values_from_config(config_data or _bootstrap_config_data(root))
        now = utcnow()
        for key, value in runtime_values.items():
            if key not in missing_keys:
                continue
            connection.execute(
                """
                INSERT INTO runtime_settings (key, value_json, updated_at, actor, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (key, _json_dumps(value), now, "system", "config_bootstrap"),
            )
        connection.commit()


def load_runtime_settings(root: Path) -> dict[str, Any]:
    bootstrap_runtime_settings(root)
    with connect_workspace_db(root) as connection:
        rows = _load_setting_rows(connection)
    return {key: rows[key] for key in RUNTIME_SETTING_KEYS if key in rows}


def apply_runtime_settings_to_config_data(root: Path, config_data: Mapping[str, Any]) -> dict[str, Any]:
    bootstrap_runtime_settings(root, config_data)
    with connect_workspace_db(root) as connection:
        settings = _load_setting_rows(connection)
    effective = dict(config_data)
    for key in RUNTIME_SETTING_KEYS:
        if key in settings:
            effective[key] = settings[key]
    return effective


def set_runtime_setting(
    root: Path,
    *,
    key: str,
    value: Any,
    actor: str,
    source: str,
    context: Mapping[str, Any] | None = None,
) -> RuntimeSettingChange:
    if key not in RUNTIME_SETTING_KEYS:
        raise ValueError(f"unsupported runtime setting {key!r}")
    bootstrap_runtime_settings(root)
    now = utcnow()
    new_json = _json_dumps(value)
    with connect_workspace_db(root) as connection:
        row = connection.execute("SELECT value_json FROM runtime_settings WHERE key = ?", (key,)).fetchone()
        old_json = None if row is None else str(row["value_json"])
        old_value = _json_loads(old_json)
        if old_value == value:
            return RuntimeSettingChange(key=key, old_value=old_value, new_value=value, changed=False)
        connection.execute(
            """
            INSERT INTO runtime_settings (key, value_json, updated_at, actor, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at,
                actor = excluded.actor,
                source = excluded.source
            """,
            (key, new_json, now, actor, source),
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
                key,
                now,
                actor,
                source,
                old_json,
                new_json,
                _json_dumps(dict(context or {})),
            ),
        )
        connection.commit()
    return RuntimeSettingChange(key=key, old_value=old_value, new_value=value, changed=True)


def set_default_engine(
    root: Path,
    engine_name: str,
    *,
    actor: str = "operator",
    source: str = "cli",
    context: Mapping[str, Any] | None = None,
) -> RuntimeSettingChange:
    return set_runtime_setting(
        root,
        key="default_engine",
        value=engine_name,
        actor=actor,
        source=source,
        context=context,
    )


def set_engine_preference(
    root: Path,
    engines: Sequence[str],
    *,
    actor: str = "operator",
    source: str = "cli",
    context: Mapping[str, Any] | None = None,
) -> RuntimeSettingChange:
    return set_runtime_setting(
        root,
        key="engine_preference",
        value=normalize_engine_sequence(engines, field_name="engine_preference"),
        actor=actor,
        source=source,
        context=context,
    )


def set_engine_freeze(
    root: Path,
    *,
    engine_name: str,
    freeze_iso: str,
    actor: str = "operator",
    source: str = "cli",
    context: Mapping[str, Any] | None = None,
) -> RuntimeSettingChange:
    settings = load_runtime_settings(root)
    freeze_map = _freeze_value(settings.get("engine_freeze", {}))
    old_engine_value = freeze_map.get(engine_name)
    freeze_map[engine_name] = freeze_iso
    return set_runtime_setting(
        root,
        key="engine_freeze",
        value=freeze_map,
        actor=actor,
        source=source,
        context={
            "engine": engine_name,
            "old_value": old_engine_value,
            "new_value": freeze_iso,
            **dict(context or {}),
        },
    )


def clear_engine_freeze(
    root: Path,
    *,
    engine_name: str,
    actor: str = "operator",
    source: str = "cli",
    context: Mapping[str, Any] | None = None,
) -> RuntimeSettingChange:
    settings = load_runtime_settings(root)
    freeze_map = _freeze_value(settings.get("engine_freeze", {}))
    old_engine_value = freeze_map.get(engine_name)
    if old_engine_value is None:
        return RuntimeSettingChange(
            key="engine_freeze",
            old_value=freeze_map,
            new_value=freeze_map,
            changed=False,
        )
    freeze_map.pop(engine_name)
    return set_runtime_setting(
        root,
        key="engine_freeze",
        value=freeze_map,
        actor=actor,
        source=source,
        context={
            "engine": engine_name,
            "old_value": old_engine_value,
            "new_value": None,
            **dict(context or {}),
        },
    )


def load_runtime_setting_audit_entries(
    root: Path,
    *,
    key: str | None = None,
    limit: int = 20,
) -> list[RuntimeSettingAuditEntry]:
    query = """
        SELECT id, key, created_at, actor, source, old_value_json, new_value_json, context_json
        FROM runtime_settings_audit_log
    """
    params: list[Any] = []
    if key is not None:
        query += " WHERE key = ?"
        params.append(key)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with connect_workspace_db(root) as connection:
        rows = connection.execute(query, params).fetchall()

    entries: list[RuntimeSettingAuditEntry] = []
    for row in rows:
        context = _json_loads(str(row["context_json"]))
        entries.append(
            RuntimeSettingAuditEntry(
                id=int(row["id"]),
                key=str(row["key"]),
                created_at=str(row["created_at"]),
                actor=str(row["actor"]),
                source=str(row["source"]),
                old_value=_json_loads(None if row["old_value_json"] is None else str(row["old_value_json"])),
                new_value=_json_loads(None if row["new_value_json"] is None else str(row["new_value_json"])),
                context=context if isinstance(context, dict) else {},
            )
        )
    return entries
