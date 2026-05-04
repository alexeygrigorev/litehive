"""Config loading and merge helpers."""

from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

import yaml
from litehive.config.model import LitehiveConfig, validate_config_data
from litehive.config.paths import litehive_root
from litehive.config.workspace import ensure_workspace
from litehive.config.workspace_files import config_path, context_path


def _read_config_layer(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return dict(payload)


def merge_config_layers(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        merged[key] = (
            merge_config_layers(current, value)
            if isinstance(current, Mapping) and isinstance(value, Mapping)
            else value
        )
    return merged


def load_effective_config_data(root: Path) -> dict[str, Any]:
    config = asdict(LitehiveConfig())
    config = merge_config_layers(config, _read_config_layer(litehive_root() / "config.yaml"))
    return merge_config_layers(config, _read_config_layer(config_path(root)))


def load_config(root: Path) -> LitehiveConfig:
    ensure_workspace(root)
    # inline: runtime_settings transitively pulls db.schema which loads
    # config.* back through litehive/config/__init__.py during partial init.
    from litehive.config.runtime_settings import apply_runtime_settings_to_config_data

    data = apply_runtime_settings_to_config_data(root, load_effective_config_data(root))
    return LitehiveConfig(**validate_config_data(data))


def load_context(root: Path) -> str:
    ensure_workspace(root)
    return context_path(root).read_text(encoding="utf-8")
