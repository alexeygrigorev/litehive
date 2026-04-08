"""Filesystem paths for workspace and global Litehive config."""

import os
from pathlib import Path


def workspace_dir(root: Path) -> Path:
    return root / ".litehive"


def config_path(root: Path) -> Path:
    return workspace_dir(root) / "config.yaml"


def global_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "litehive" / "config.yaml"
    return Path.home() / ".config" / "litehive" / "config.yaml"


def daemon_config_dir() -> Path:
    return global_config_path().parent


def daemon_registry_path() -> Path:
    return daemon_config_dir() / "daemons.yaml"


def state_path(root: Path) -> Path:
    return workspace_dir(root) / "state.yaml"


def context_path(root: Path) -> Path:
    return workspace_dir(root) / "context.md"


def workspace_gitignore_path(root: Path) -> Path:
    return workspace_dir(root) / ".gitignore"
