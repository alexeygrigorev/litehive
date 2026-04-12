"""Filesystem paths for workspace and global Litehive config."""

import hashlib
import os
from pathlib import Path


def workspace_dir(root: Path) -> Path:
    return root / ".litehive"


def config_path(root: Path) -> Path:
    return workspace_dir(root) / "config.yaml"


def global_config_path() -> Path:
    return litehive_config_home() / "config.yaml"


def daemon_config_dir() -> Path:
    return global_config_path().parent


def daemon_registry_path() -> Path:
    return daemon_config_dir() / "daemons.yaml"


def litehive_config_home() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "litehive"
    return Path.home() / ".config" / "litehive"


def litehive_data_home() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home) / "litehive"
    return Path.home() / ".local" / "share" / "litehive"


def litehive_database_path() -> Path:
    return litehive_data_home() / "litehive.db"


def litehive_state_home() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "litehive"
    return Path.home() / ".local" / "state" / "litehive"


def legacy_workspace_registry_path() -> Path:
    return litehive_config_home() / "workspaces.yaml"


def workspace_id(root: Path) -> str:
    canonical = str(root.expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def workspace_data_dir(root: Path) -> Path:
    return litehive_data_home() / workspace_id(root)


def workspace_state_root(root: Path) -> Path:
    return litehive_state_home() / workspace_id(root)


def workspace_database_path(root: Path) -> Path:
    return workspace_data_dir(root) / "data.db"


def workspace_backups_dir(root: Path) -> Path:
    return workspace_data_dir(root) / "backups"


def workspace_logs_dir(root: Path) -> Path:
    return workspace_state_root(root) / "logs"


def workspace_worktrees_dir(root: Path) -> Path:
    return workspace_state_root(root) / "worktrees"


def worktree_root(root: Path) -> Path:
    return workspace_worktrees_dir(root)


def workspace_subagents_dir(
    root: Path,
    task_id: str | None = None,
    subagent_id: str | None = None,
) -> Path:
    path = workspace_data_dir(root) / "subagents"
    if task_id is not None:
        path /= task_id
    if subagent_id is not None:
        path /= subagent_id
    return path


def state_path(root: Path) -> Path:
    return workspace_dir(root) / "state.yaml"


def context_path(root: Path) -> Path:
    return workspace_dir(root) / "context.md"


def workspace_gitignore_path(root: Path) -> Path:
    return workspace_dir(root) / ".gitignore"
