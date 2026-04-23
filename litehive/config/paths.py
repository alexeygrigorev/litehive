"""Filesystem paths for repo-local and unified global Litehive state."""

import hashlib
import os
from pathlib import Path


def _xdg_config_litehive_root() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "litehive"
def litehive_config_root() -> Path:
    override = os.environ.get("LITEHIVE_HOME")
    if override:
        return Path(override).expanduser()
    return _xdg_config_litehive_root()


def _configured_litehive_root() -> Path:
    override = os.environ.get("LITEHIVE_HOME")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "litehive"
def litehive_root() -> Path:
    root = _configured_litehive_root().expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _workspace_id(root: Path) -> str:
    canonical = str(root.expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def workspace_data_dir(root: Path) -> Path:
    return litehive_root() / _workspace_id(root)


def workspace_path(root: Path, *parts: str) -> Path:
    return workspace_data_dir(root).joinpath(*parts)
