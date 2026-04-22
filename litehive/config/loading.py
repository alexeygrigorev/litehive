"""Config loading and merge helpers."""

from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

import yaml

from litehive.config.model import LitehiveConfig, validate_config_data
from litehive.config.paths import litehive_root
from litehive.config.workspace_files import config_path, context_path
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
    for path in (litehive_root() / "config.yaml", config_path(root)):
        data = merge_config_layers(data, read_config_mapping(path))
    return data


def load_config(root: Path) -> LitehiveConfig:
    ensure_workspace(root)
    return LitehiveConfig(**validate_config_data(load_effective_config_data(root)))


def load_context(root: Path) -> str:
    ensure_workspace(root)
    return context_path(root).read_text(encoding="utf-8")
