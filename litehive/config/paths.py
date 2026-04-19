"""Filesystem paths for repo-local and unified global Litehive state."""

import hashlib
import os
from pathlib import Path


def workspace_dir(root: Path) -> Path:
    return root / ".litehive"


def config_path(root: Path) -> Path:
    return workspace_dir(root) / "config.yaml"


def context_path(root: Path) -> Path:
    return workspace_dir(root) / "context.md"


def workspace_gitignore_path(root: Path) -> Path:
    return workspace_dir(root) / ".gitignore"


def litehive_root() -> Path:
    override = os.environ.get("LITEHIVE_HOME")
    if override:
        root = Path(override).expanduser()
    else:
        data_home = os.environ.get("XDG_DATA_HOME")
        root = (Path(data_home) if data_home else Path.home() / ".local" / "share") / "litehive"
    return root.expanduser()


def global_config_path() -> Path:
    return litehive_root() / "config.yaml"


def litehive_database_path() -> Path:
    return litehive_root() / "litehive.db"


def workspace_registry_path() -> Path:
    override = os.environ.get("LITEHIVE_HOME")
    if override:
        return Path(override).expanduser() / "workspaces.yaml"
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "litehive" / "workspaces.yaml"
    return Path.home() / ".litehive" / "workspaces.yaml"


def workspace_id(root: Path) -> str:
    canonical = str(root.expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def workspace_data_dir(root: Path) -> Path:
    return litehive_root() / workspace_id(root)


def workspace_runtime_dir(root: Path) -> Path:
    return workspace_data_dir(root) / "runtime"


def workspace_database_path(root: Path) -> Path:
    return workspace_data_dir(root) / "data.db"


def workspace_backups_dir(root: Path) -> Path:
    return workspace_data_dir(root) / "backups"


def workspace_logs_dir(root: Path) -> Path:
    return workspace_data_dir(root) / "logs"


def workspace_worktrees_dir(root: Path) -> Path:
    return workspace_data_dir(root) / "worktrees"


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


def workspace_runner_lock_path(root: Path) -> Path:
    return workspace_runtime_dir(root) / ".runner.lock"


def workspace_daemon_lock_path(root: Path) -> Path:
    return workspace_runtime_dir(root) / ".daemon.lock"
