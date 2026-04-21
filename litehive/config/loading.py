"""Config loading and merge helpers."""

from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Mapping

import yaml

from litehive.config.model import LitehiveConfig
from litehive.config.paths import litehive_root
from litehive.config.profiles.loader import PROCESS_PROFILES
from litehive.config.workspace_files import config_path, context_path
from litehive.config.workspace import ensure_workspace

_LEGACY_UNSUPPORTED_KEYS = {
    "pre_acceptance_command": (
        "pre_acceptance_command is no longer supported. Migrate this command to runner_hooks.before_accepting."
    ),
    "task_engine_routing": (
        "task_engine_routing is no longer supported. "
        "Engine selection now comes only from default_engine, explicit runtime engine switches, "
        "or CLI --engine."
    ),
    "engine_fallbacks": (
        "engine_fallbacks is no longer supported. "
        "Use engine_preference to specify engine ordering instead."
    ),
    "runner_hook_execution_mode": (
        "runner_hook_execution_mode is no longer supported. "
        "Hook execution mode is now always synchronous."
    ),
}


def read_config_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return dict(data)


def merge_config_layers(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = merge_config_layers(dict(merged[key]), value)
            continue
        merged[key] = value
    return merged


def load_effective_config_data(root: Path) -> dict[str, Any]:
    data = asdict(LitehiveConfig())
    for path in (litehive_root() / "config.yaml", config_path(root)):
        data = merge_config_layers(data, read_config_mapping(path))
    return data




def load_config(root: Path) -> LitehiveConfig:
    ensure_workspace(root)
    data = load_effective_config_data(root)
    for key, message in _LEGACY_UNSUPPORTED_KEYS.items():
        if key in data:
            raise ValueError(message)
    if "process_profile" in data and data["process_profile"] not in PROCESS_PROFILES:
        available_profiles = ", ".join(sorted(PROCESS_PROFILES.keys()))
        raise ValueError(f"unknown process_profile {data['process_profile']!r}; must be one of: {available_profiles}")
    valid_keys = {f.name for f in fields(LitehiveConfig)}
    for key in list(data):
        if key not in valid_keys:
            raise ValueError(f"unknown config key {key!r}; remove it or migrate to a supported config field")
    return LitehiveConfig(**data)


def load_context(root: Path) -> str:
    ensure_workspace(root)
    return context_path(root).read_text(encoding="utf-8")
