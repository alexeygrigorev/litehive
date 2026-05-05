"""Audited runtime settings stored in the workspace database."""

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

import yaml

from litehive.config.model import LitehiveConfig, normalize_engine_sequence
from litehive.config.paths import litehive_root
from litehive.config.workspace_files import config_path
from litehive.domain.common import utcnow
from litehive.workspace import Workspace

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
    """Load one ``config.yaml`` layer (global or workspace) into a dict, returning ``{}`` for missing files and raising ``ValueError`` for non-mapping payloads; consumed by :func:`_bootstrap_config_data` so the two layers can be merged uniformly."""
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return dict(payload)


def _merge_config_layers(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge two config dicts so workspace YAML overrides global YAML override dataclass defaults at the leaf level; called by :func:`_bootstrap_config_data` to compose the runtime-settings seed without losing nested structure."""
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_config_layers(current, value)
        else:
            merged[key] = value
    return merged


def _bootstrap_config_data(workspace: Workspace) -> dict[str, Any]:
    """Compose the three-layer config snapshot (dataclass defaults < global YAML < workspace YAML) used to seed runtime settings on first access; called by :func:`bootstrap_runtime_settings` so the database starts out with the same values an operator would see by reading the YAML files directly."""
    config = asdict(LitehiveConfig())
    config = _merge_config_layers(config, _read_config_layer(litehive_root() / "config.yaml"))
    return _merge_config_layers(config, _read_config_layer(config_path(workspace.root)))


def _json_dumps(value: Any) -> str:
    """Serialise a runtime-setting value with sorted keys so two semantically-equal dicts always produce byte-equal JSON; this is what makes the no-op short-circuit in :func:`set_runtime_setting` reliable."""
    return json.dumps(value, sort_keys=True)


def _json_loads(raw: str | None) -> Any:
    """Decode a stored ``value_json`` column tolerantly, returning None for both NULL rows and malformed JSON; lets the audit-log readers and the no-op check survive a single corrupt row instead of erroring on every read."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _sequence_value(raw_value: Any, field_name: str) -> list[str]:
    """Coerce an engine-preference-shaped value into a normalised list of engine names, rejecting strings or non-sequence inputs up front; called by :func:`_runtime_values_from_config` so a typo in YAML fails loudly at bootstrap rather than silently at engine-selection time."""
    if raw_value is None:
        return []
    if isinstance(raw_value, str) or not isinstance(raw_value, Sequence):
        raise ValueError(f"{field_name} must be a sequence of engine names")
    return normalize_engine_sequence([str(item) for item in raw_value], field_name=field_name)


def _freeze_value(raw_value: Any) -> dict[str, str]:
    """Coerce a freeze-map-shaped value into ``{engine: iso_string}``, raising on non-mappings; called by :func:`_runtime_values_from_config` and the freeze CLI helpers so the in-memory freeze map always has predictable types regardless of the YAML author's intent."""
    if raw_value is None:
        return {}
    if not isinstance(raw_value, Mapping):
        raise ValueError("engine_freeze must be a mapping of engine name to UTC timestamp")
    return {str(key): str(value) for key, value in raw_value.items()}


def _runtime_values_from_config(config_data: Mapping[str, Any]) -> dict[str, Any]:
    """Project a merged config dict down to the three audited keys (``default_engine``, ``engine_preference``, ``engine_freeze``), normalising each through its type coercer; the seed value source for :func:`bootstrap_runtime_settings`."""
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
    """Read every row from the ``runtime_settings`` table into a ``{key: parsed_value}`` dict, tolerating malformed JSON as None; the shared row reader behind :func:`load_runtime_settings` and :func:`apply_runtime_settings_to_config_data`."""
    rows = connection.execute("SELECT key, value_json FROM runtime_settings").fetchall()
    return {str(row["key"]): _json_loads(str(row["value_json"])) for row in rows}


def bootstrap_runtime_settings(workspace: Workspace, config_data: Mapping[str, Any] | None = None) -> None:
    """Seed audited runtime settings from config files once.

    The config file values are bootstrap-only. After the corresponding database
    rows exist, later config-file drift is ignored by runtime config loading.
    """
    with workspace.connect() as connection:
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
        runtime_values = _runtime_values_from_config(config_data or _bootstrap_config_data(workspace))
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


def load_runtime_settings(workspace: Workspace) -> dict[str, Any]:
    """Return the current audited values for the engine-control settings, bootstrapping from config files on first access; called by the CLI ``runtime-settings show`` surface and by anywhere that needs the post-bootstrap view without merging it back into the full ``LitehiveConfig`` shape."""
    bootstrap_runtime_settings(workspace)
    with workspace.connect() as connection:
        rows = _load_setting_rows(connection)
    return {key: rows[key] for key in RUNTIME_SETTING_KEYS if key in rows}


def apply_runtime_settings_to_config_data(workspace: Workspace, config_data: Mapping[str, Any]) -> dict[str, Any]:
    """Overlay the audited runtime values on top of file-loaded config data and return a merged dict; called by config loading so that once a setting is in the database the file value is ignored, making the database the source of truth after bootstrap."""
    bootstrap_runtime_settings(workspace, config_data)
    with workspace.connect() as connection:
        settings = _load_setting_rows(connection)
    effective = dict(config_data)
    for key in RUNTIME_SETTING_KEYS:
        if key in settings:
            effective[key] = settings[key]
    return effective


def set_runtime_setting(
    workspace: Workspace,
    key: str,
    value: Any,
    actor: str,
    source: str,
    context: Mapping[str, Any] | None = None,
) -> RuntimeSettingChange:
    """Write a single audited setting and append an audit-log row in the same transaction, returning a no-op ``RuntimeSettingChange`` when the value is unchanged; this is the one entry point through which every CLI/quota/recovery mutation must go so the audit log stays complete."""
    if key not in RUNTIME_SETTING_KEYS:
        raise ValueError(f"unsupported runtime setting {key!r}")
    bootstrap_runtime_settings(workspace)
    now = utcnow()
    new_json = _json_dumps(value)
    with workspace.connect() as connection:
        row = connection.execute("SELECT value_json FROM runtime_settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            old_json = None
        else:
            old_json = str(row["value_json"])
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
    workspace: Workspace,
    engine_name: str,
    actor: str = "operator",
    source: str = "cli",
    context: Mapping[str, Any] | None = None,
) -> RuntimeSettingChange:
    """Persist the workspace's default engine through the audited store; called by the CLI ``engine default`` command (``actor='operator'``) and intentionally typed as a thin wrapper so the audit trail records the semantic intent, not just a raw key write."""
    return set_runtime_setting(
        workspace,
        key="default_engine",
        value=engine_name,
        actor=actor,
        source=source,
        context=context,
    )


def set_engine_preference(
    workspace: Workspace,
    engines: Sequence[str],
    actor: str = "operator",
    source: str = "cli",
    context: Mapping[str, Any] | None = None,
) -> RuntimeSettingChange:
    """Persist the engine fallback order through the audited store, normalising the sequence first so duplicate or empty entries are rejected up front; called by the CLI ``engine preference`` command."""
    return set_runtime_setting(
        workspace,
        key="engine_preference",
        value=normalize_engine_sequence(engines, field_name="engine_preference"),
        actor=actor,
        source=source,
        context=context,
    )


def set_engine_freeze(
    workspace: Workspace,
    engine_name: str,
    freeze_iso: str,
    actor: str = "operator",
    source: str = "cli",
    context: Mapping[str, Any] | None = None,
) -> RuntimeSettingChange:
    """Add or refresh one engine's freeze-until timestamp inside the freeze map and persist the whole map atomically; called by the CLI ``engine freeze`` command and by the engine-selection loop when a quota response carries a reset time, with the per-engine before/after values written into the audit context for easy diffing."""
    settings = load_runtime_settings(workspace)
    freeze_map = _freeze_value(settings.get("engine_freeze", {}))
    old_engine_value = freeze_map.get(engine_name)
    freeze_map[engine_name] = freeze_iso
    return set_runtime_setting(
        workspace,
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
    workspace: Workspace,
    engine_name: str,
    actor: str = "operator",
    source: str = "cli",
    context: Mapping[str, Any] | None = None,
) -> RuntimeSettingChange:
    """Remove a single engine's entry from the freeze map and persist the rest, returning an unchanged ``RuntimeSettingChange`` when the engine was already unfrozen so callers can avoid noisy "no-op" log lines; called by the CLI ``engine unfreeze`` command and by the engine-selection loop when a freeze window has expired."""
    settings = load_runtime_settings(workspace)
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
        workspace,
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
    workspace: Workspace,
    key: str | None = None,
    limit: int = 20,
) -> list[RuntimeSettingAuditEntry]:
    """Return the most recent audit-log rows, newest-first, optionally filtered to one setting key; consumed by the CLI ``runtime-settings audit`` view that operators use to answer "who changed the engine freeze and when"."""
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

    with workspace.connect() as connection:
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
