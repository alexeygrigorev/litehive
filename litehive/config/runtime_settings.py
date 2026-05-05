"""
Audited runtime settings stored in the workspace database.

Owns three engine-control settings — ``default_engine``,
``engine_preference``, ``engine_freeze`` — so every change leaves
an audit trail. The database is the source of truth after
bootstrap; ``config.yaml`` only seeds the initial values, and
later YAML drift is intentionally ignored to avoid two writers.
"""

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
    """
    Load one ``config.yaml`` layer (global or workspace).

    Returns ``{}`` for missing files and raises ``ValueError``
    for non-mapping payloads. Consumed by
    :func:`_bootstrap_config_data` so the two layers can be merged
    uniformly. Duplicates :func:`litehive.config.loading._read_config_layer`
    intentionally so this module can run in the partial-import
    window where ``config.loading`` is not yet ready.
    """
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return dict(payload)


def _merge_config_layers(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """
    Deep-merge two config dicts at the leaf level.

    Used by :func:`_bootstrap_config_data` so workspace YAML
    overrides global YAML overrides dataclass defaults without
    losing nested structure. A shallow merge would clobber an
    operator's partially-set nested key (e.g. one freeze entry).
    """
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_config_layers(current, value)
        else:
            merged[key] = value
    return merged


def _bootstrap_config_data(workspace: Workspace) -> dict[str, Any]:
    """
    Compose the three-layer config snapshot used to seed runtime settings.

    Order: dataclass defaults < global YAML < workspace YAML.
    Called by :func:`bootstrap_runtime_settings` so the database
    starts out with the same values an operator would see by
    reading the YAML files directly — the operator's mental model
    matches the stored state from day one.
    """
    config = asdict(LitehiveConfig())
    config = _merge_config_layers(config, _read_config_layer(litehive_root() / "config.yaml"))
    return _merge_config_layers(config, _read_config_layer(config_path(workspace.root)))


def _json_dumps(value: Any) -> str:
    """
    Serialise a runtime-setting value with sorted keys.

    Sorting on the way out means two semantically-equal dicts
    produce byte-equal JSON — a precondition for the no-op
    short-circuit in :func:`set_runtime_setting` (otherwise an
    unrelated key reorder would look like a change and write a
    spurious audit row).
    """
    return json.dumps(value, sort_keys=True)


def _json_loads(raw: str | None) -> Any:
    """
    Decode a stored ``value_json`` column tolerantly.

    Returns ``None`` for both NULL rows and malformed JSON so
    the audit-log readers and the no-op check survive a single
    corrupt row instead of erroring on every read; the alternative
    would block every settings operation behind one bad row.
    """
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _sequence_value(raw_value: Any, field_name: str) -> list[str]:
    """
    Coerce an engine-preference value into a normalised list of names.

    Rejects strings or non-sequence inputs up front so a typo in
    YAML fails loudly at bootstrap rather than silently at
    engine-selection time. Without this guard a single string
    would look like a sequence-of-characters at runtime.
    """
    if raw_value is None:
        return []
    if isinstance(raw_value, str) or not isinstance(raw_value, Sequence):
        raise ValueError(f"{field_name} must be a sequence of engine names")
    return normalize_engine_sequence([str(item) for item in raw_value], field_name=field_name)


def _freeze_value(raw_value: Any) -> dict[str, str]:
    """
    Coerce a freeze-map value into ``{engine: iso_string}``.

    Raises on non-mappings so the in-memory freeze map always has
    predictable types regardless of how the YAML author wrote it.
    Used by :func:`_runtime_values_from_config` and the freeze
    CLI helpers — without this all callers would have to repeat
    the same type checks.
    """
    if raw_value is None:
        return {}
    if not isinstance(raw_value, Mapping):
        raise ValueError("engine_freeze must be a mapping of engine name to UTC timestamp")
    return {str(key): str(value) for key, value in raw_value.items()}


def _runtime_values_from_config(config_data: Mapping[str, Any]) -> dict[str, Any]:
    """
    Project a merged config dict down to the three audited keys.

    Returns ``default_engine``, ``engine_preference``, and
    ``engine_freeze`` after running each through its type
    coercer. The seed value source for
    :func:`bootstrap_runtime_settings`; centralising the
    projection keeps the bootstrap and the operator-visible
    settings in lockstep.
    """
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
    """
    Read every ``runtime_settings`` row into a ``{key: parsed_value}`` dict.

    Tolerates malformed JSON as ``None`` so a single bad row
    does not block the rest of the settings. Shared row reader
    behind :func:`load_runtime_settings` and
    :func:`apply_runtime_settings_to_config_data` — the two
    callers want the same parsed shape, so the parsing lives here
    rather than at each call site.
    """
    rows = connection.execute("SELECT key, value_json FROM runtime_settings").fetchall()
    return {str(row["key"]): _json_loads(str(row["value_json"])) for row in rows}


def bootstrap_runtime_settings(workspace: Workspace, config_data: Mapping[str, Any] | None = None) -> None:
    """
    Seed audited runtime settings from the config files exactly once.

    Config-file values are bootstrap-only. After the corresponding
    database rows exist, later config-file drift is intentionally
    ignored by runtime config loading so the database stays the
    sole source of truth — having two writable surfaces would
    invite the audit log to disagree with the live setting.
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
    """
    Return the current audited values for the engine-control settings.

    Bootstraps from config files on first access so a fresh
    workspace has the right defaults without an explicit init
    step. Called by the CLI ``runtime-settings show`` surface
    and anywhere that needs the post-bootstrap view without
    merging it back into the full :class:`LitehiveConfig` shape.
    """
    bootstrap_runtime_settings(workspace)
    with workspace.connect() as connection:
        rows = _load_setting_rows(connection)
    return {key: rows[key] for key in RUNTIME_SETTING_KEYS if key in rows}


def apply_runtime_settings_to_config_data(workspace: Workspace, config_data: Mapping[str, Any]) -> dict[str, Any]:
    """
    Overlay the audited runtime values on top of file-loaded config data.

    Called by config loading so that once a setting lives in the
    database the file value is ignored — making the database the
    source of truth after bootstrap. The overlay is the seam
    between file-driven defaults and operator-driven mutations,
    and centralizing it here keeps file/database precedence in
    one place.
    """
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
    """
    Write one audited setting and append an audit-log row atomically.

    Returns a no-op :class:`RuntimeSettingChange` when the value is
    unchanged so chained callers can short-circuit without an
    extra read. The single entry point every CLI, quota, and
    recovery mutation goes through — that mandate is what keeps
    the audit log complete; an alternate write path would let
    changes slip past unnoticed.
    """
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
    """
    Persist the workspace's default engine through the audited store.

    Called by the CLI ``engine default`` command. Kept as a thin
    typed wrapper around :func:`set_runtime_setting` so the
    audit trail records the semantic intent (``set_default_engine``
    -> a particular key) rather than a raw key write that future
    readers would have to decode.
    """
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
    """
    Persist the engine fallback order through the audited store.

    Normalises the sequence first so duplicate or empty entries
    are rejected up front, before they reach the audit log; an
    audit row that records a malformed list would still be a
    valid SQL row but would mislead anyone diagnosing engine
    selection later. Called by the CLI ``engine preference``
    command.
    """
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
    """
    Add or refresh one engine's freeze-until timestamp.

    Persists the whole freeze map atomically so the audit row
    captures a consistent post-state. Called by the CLI
    ``engine freeze`` command and by the engine-selection loop
    when a quota response carries a reset time. Per-engine
    before/after values land in the audit context for easy
    diffing across runs.
    """
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
    """
    Remove a single engine's freeze entry and persist the rest.

    Returns an unchanged :class:`RuntimeSettingChange` when the
    engine was already unfrozen so callers can avoid noisy
    no-op log lines; the audit log records only real
    transitions. Called by the CLI ``engine unfreeze`` command
    and by the engine-selection loop when a freeze window has
    expired.
    """
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
    """
    Return the most recent runtime-settings audit-log rows.

    Newest-first, optionally filtered to one setting key.
    Consumed by the CLI ``runtime-settings audit`` view that
    operators use to answer "who changed the engine freeze and
    when?". Tolerantly decodes legacy/null context values so an
    early row missing the column does not break the listing.
    """
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
        if row["old_value_json"] is None:
            old_value_arg = None
        else:
            old_value_arg = str(row["old_value_json"])
        if row["new_value_json"] is None:
            new_value_arg = None
        else:
            new_value_arg = str(row["new_value_json"])
        if isinstance(context, dict):
            context_value = context
        else:
            context_value = {}
        entries.append(
            RuntimeSettingAuditEntry(
                id=int(row["id"]),
                key=str(row["key"]),
                created_at=str(row["created_at"]),
                actor=str(row["actor"]),
                source=str(row["source"]),
                old_value=_json_loads(old_value_arg),
                new_value=_json_loads(new_value_arg),
                context=context_value,
            )
        )
    return entries
