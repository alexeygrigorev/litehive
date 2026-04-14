"""Config loading and merge helpers."""

from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

import yaml

from litehive.config.model import VALID_POOL_SELECTION_POLICIES
from litehive.config.model import LitehiveConfig
from litehive.config.paths import config_path, context_path, global_config_path
from litehive.config.profiles.loader import PROCESS_PROFILES
from litehive.config.workspace import ensure_workspace


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
    for path in (global_config_path(), config_path(root)):
        data = merge_config_layers(data, read_config_mapping(path))
    return data


def load_config(root: Path) -> LitehiveConfig:
    ensure_workspace(root)
    data = load_effective_config_data(root)
    data.pop("engine_fallbacks", None)
    if data.get("process_profile") not in PROCESS_PROFILES:
        data["process_profile"] = "generic"
    if data.get("pool_selection_policy") not in VALID_POOL_SELECTION_POLICIES:
        data["pool_selection_policy"] = "dependency_aware"
    data.pop("engine_fallbacks", None)
    if data.pop("pre_acceptance_command", None):
        raise ValueError(
            "pre_acceptance_command is no longer supported. "
            "Migrate to runner_hooks.after_implementing in config.yaml. Example:\n"
            "  runner_hooks:\n"
            "    after_implementing:\n"
            "      - command: '<your command>'\n"
            "        reject_on_failure: true"
        )
    import dataclasses
    import warnings
    known_fields = {f.name for f in dataclasses.fields(LitehiveConfig)}
    for key in sorted(set(data) - known_fields):
        warnings.warn(f"unknown config key '{key}' — ignoring", stacklevel=2)
    data = {k: v for k, v in data.items() if k in known_fields}
    return LitehiveConfig(**data)


def load_context(root: Path) -> str:
    ensure_workspace(root)
    return context_path(root).read_text(encoding="utf-8")
