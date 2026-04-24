"""Legacy global-state migration into the unified Litehive data root."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
import threading

import yaml

log = logging.getLogger(__name__)

_MIGRATION_MUTEX = threading.Lock()


def legacy_litehive_root() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "litehive"


def migrate_legacy_global_state(root: Path) -> None:
    root = root.expanduser()
    legacy_root = legacy_litehive_root().expanduser()
    if legacy_root == root:
        return

    migrated_labels: list[str] = []
    with _MIGRATION_MUTEX:
        if legacy_root == root:
            return
        config_migrated = _migrate_global_yaml_mapping(
            legacy_root / "config.yaml",
            root / "config.yaml",
        )
        if config_migrated:
            migrated_labels.append("config.yaml")

        daemons_migrated = _migrate_daemon_registry_file(
            legacy_root / "daemons.yaml",
            root / "daemons.yaml",
        )
        if daemons_migrated:
            migrated_labels.append("daemons.yaml")

        from litehive.config.registry import migrate_legacy_registry_file

        if migrate_legacy_registry_file(root / "workspaces.db", legacy_root / "workspaces.yaml"):
            migrated_labels.append("workspaces.yaml")

    if migrated_labels:
        joined = ", ".join(migrated_labels)
        print(
            (
                f"[litehive] migrated deprecated global state from {legacy_root} to {root} "
                f"({joined}); Litehive now uses {root} for all global state."
            ),
            file=sys.stderr,
        )


def _migrate_global_yaml_mapping(legacy_path: Path, target_path: Path) -> bool:
    return _migrate_yaml_file(legacy_path, target_path, merger=_merge_yaml_mappings)


def _migrate_daemon_registry_file(legacy_path: Path, target_path: Path) -> bool:
    return _migrate_yaml_file(legacy_path, target_path, merger=_merge_daemon_registry_entries)


def _migrate_yaml_file(
    legacy_path: Path,
    target_path: Path,
    *,
    merger,
) -> bool:
    if not legacy_path.exists():
        return False
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not target_path.exists():
            legacy_path.replace(target_path)
            return True
        if legacy_path.read_bytes() == target_path.read_bytes():
            legacy_path.unlink(missing_ok=True)
            return True
        if merger(legacy_path, target_path):
            legacy_path.unlink(missing_ok=True)
            return True
        legacy_path.replace(_legacy_conflict_target(target_path))
        return True
    except OSError as exc:
        log.warning("failed to migrate legacy global state %s to %s (%s)", legacy_path, target_path, exc)
        return False


def _merge_yaml_mappings(legacy_path: Path, target_path: Path) -> bool:
    legacy_payload = _load_yaml_mapping(legacy_path)
    target_payload = _load_yaml_mapping(target_path)
    if legacy_payload is None or target_payload is None:
        return False
    merged = _deep_merge(legacy_payload, target_payload)
    target_path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    return True


def _merge_daemon_registry_entries(legacy_path: Path, target_path: Path) -> bool:
    legacy_payload = _load_yaml_list(legacy_path)
    target_payload = _load_yaml_list(target_path)
    if legacy_payload is None or target_payload is None:
        return False

    indexed: dict[str, dict[str, object]] = {}
    passthrough: list[dict[str, object]] = []
    for entry in legacy_payload + target_payload:
        workspace = entry.get("workspace")
        if isinstance(workspace, str):
            indexed[workspace] = dict(entry)
            continue
        passthrough.append(dict(entry))

    merged = sorted(indexed.values(), key=lambda item: str(item.get("workspace", "")))
    merged.extend(passthrough)
    target_path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    return True


def _load_yaml_mapping(path: Path) -> dict[str, object] | None:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(payload, dict):
        return None
    return dict(payload)


def _load_yaml_list(path: Path) -> list[dict[str, object]] | None:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(payload, list):
        return None
    items = [dict(entry) for entry in payload if isinstance(entry, dict)]
    if len(items) != len(payload):
        return None
    return items


def _deep_merge(base: dict[str, object], overlay: dict[str, object]) -> dict[str, object]:
    merged: dict[str, object] = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
            continue
        merged[key] = value
    return merged


def _legacy_conflict_target(target_path: Path) -> Path:
    candidate = target_path.with_name(f"{target_path.stem}.legacy{target_path.suffix}")
    counter = 2
    while candidate.exists():
        candidate = target_path.with_name(f"{target_path.stem}.legacy-{counter}{target_path.suffix}")
        counter += 1
    return candidate
