"""Filesystem paths for repo-local and unified global Litehive state."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import sys


def workspace_dir(root: Path) -> Path:
    return root / ".litehive"


def config_path(root: Path) -> Path:
    return workspace_dir(root) / "config.yaml"


def state_path(root: Path) -> Path:
    return workspace_dir(root) / "state.yaml"


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
    return _migrate_legacy_layout(root)


def global_config_path() -> Path:
    return litehive_root() / "config.yaml"


def daemon_registry_path() -> Path:
    return litehive_root() / "daemons.yaml"


def litehive_database_path() -> Path:
    return litehive_root() / "litehive.db"


def legacy_workspace_registry_path() -> Path:
    return _legacy_config_root() / "workspaces.yaml"


def workspace_id(root: Path) -> str:
    canonical = str(root.expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def workspace_data_dir(root: Path) -> Path:
    return litehive_root() / workspace_id(root)


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


def legacy_global_config_path() -> Path:
    return _legacy_config_root() / "config.yaml"


def legacy_daemon_registry_path() -> Path:
    return _legacy_config_root() / "daemons.yaml"


def legacy_workspace_state_dir(root: Path) -> Path:
    return _legacy_state_root() / workspace_id(root)


def legacy_workspace_logs_dir(root: Path) -> Path:
    return legacy_workspace_state_dir(root) / "logs"


def legacy_workspace_worktrees_dir(root: Path) -> Path:
    return legacy_workspace_state_dir(root) / "worktrees"


def legacy_repo_logs_dir(root: Path) -> Path:
    return workspace_dir(root) / "logs"


def legacy_repo_worktrees_dir(root: Path) -> Path:
    return workspace_dir(root) / "worktrees"


def _legacy_config_root() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "litehive"
    return Path.home() / ".config" / "litehive"


def _legacy_state_root() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "litehive"
    return Path.home() / ".local" / "state" / "litehive"


def _migration_notice_marker(root: Path) -> Path:
    return root / ".t0342-migrated"


def _migrate_legacy_layout(root: Path) -> Path:
    root = root.expanduser()
    migrated: list[tuple[Path, Path]] = []

    _copy_if_missing(legacy_global_config_path(), root / "config.yaml", migrated)
    _copy_if_missing(legacy_daemon_registry_path(), root / "daemons.yaml", migrated)

    _emit_migration_notice(root, migrated)
    return root


def migrate_legacy_workspace_state(root: Path) -> list[tuple[Path, Path]]:
    resolved_root = root.expanduser().resolve()
    migrated: list[tuple[Path, Path]] = []
    _copy_tree_missing(legacy_workspace_state_dir(resolved_root), workspace_data_dir(resolved_root), migrated)
    _copy_tree_missing(legacy_repo_logs_dir(resolved_root), workspace_logs_dir(resolved_root), migrated)
    _copy_tree_missing(legacy_repo_worktrees_dir(resolved_root), workspace_worktrees_dir(resolved_root), migrated)
    _emit_migration_notice(litehive_root(), migrated)
    return migrated


def _emit_migration_notice(root: Path, migrated: list[tuple[Path, Path]]) -> None:
    marker = _migration_notice_marker(root)
    if not migrated or marker.exists():
        return
    root.mkdir(parents=True, exist_ok=True)
    notice = (
        "litehive: migrated legacy state into "
        f"{root}. ~/.config/litehive, ~/.local/state/litehive, and old repo-local "
        "runtime paths are now deprecated compatibility sources; use LITEHIVE_HOME "
        "or the unified data root instead.\n"
    )
    sys.stderr.write(notice)
    sys.stderr.flush()
    marker.write_text("t0342_migration_notice_emitted: true\n", encoding="utf-8")


def _copy_if_missing(source: Path, destination: Path, migrated: list[tuple[Path, Path]]) -> None:
    try:
        if not source.exists() or destination.exists():
            return
    except OSError:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    migrated.append((source, destination))


def _copy_tree_missing(source: Path, destination: Path, migrated: list[tuple[Path, Path]]) -> None:
    try:
        if not source.exists():
            return
    except OSError:
        return
    if source.is_file():
        _copy_if_missing(source, destination, migrated)
        return
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            _copy_tree_missing(child, target, migrated)
        else:
            _copy_if_missing(child, target, migrated)
