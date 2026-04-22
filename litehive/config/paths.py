"""Filesystem paths for repo-local and unified global Litehive state."""

import hashlib
import os
from pathlib import Path
import shutil
import sys

_LEGACY_GLOBAL_FILENAMES = ("config.yaml", "daemons.yaml")


def _xdg_config_litehive_root() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "litehive"


def _legacy_litehive_root() -> Path:
    return _xdg_config_litehive_root()


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


def _print_migration_notice(root: Path, migrated: list[tuple[Path, Path]]) -> None:
    if not migrated:
        return
    print(
        "litehive: migrated legacy global state into "
        f"{root}. The ~/.config/litehive location is deprecated.",
        file=sys.stderr,
    )
    for source, target in migrated:
        print(f"  {source} -> {target}", file=sys.stderr)


def _migration_marker_path(root: Path) -> Path:
    return root / ".legacy-global-state-migrated"


def _files_match(left: Path, right: Path) -> bool:
    if not left.exists() or not right.exists():
        return False
    try:
        return left.read_bytes() == right.read_bytes()
    except OSError:
        return False


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _migrate_legacy_global_state(root: Path) -> None:
    legacy_root = _legacy_litehive_root()
    migrated: list[tuple[Path, Path]] = []
    migration_marker = _migration_marker_path(root)
    first_migration = not migration_marker.exists()
    if legacy_root.resolve() != root.resolve():
        for filename in _LEGACY_GLOBAL_FILENAMES:
            source = legacy_root / filename
            target = root / filename
            if not source.exists():
                continue
            try:
                if first_migration:
                    if not target.exists() or not _files_match(source, target):
                        _copy_file(source, target)
                    migrated.append((source, target))
                    continue
                if not target.exists():
                    _copy_file(source, target)
                    continue
                if not _files_match(source, target):
                    _copy_file(target, source)
            except OSError:
                continue
        if first_migration and migrated:
            migration_marker.write_text("legacy global state migrated\n", encoding="utf-8")
    _print_migration_notice(root, migrated)


def litehive_root() -> Path:
    root = _configured_litehive_root().expanduser()
    root.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_global_state(root)
    return root


def _workspace_id(root: Path) -> str:
    canonical = str(root.expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def workspace_data_dir(root: Path) -> Path:
    return litehive_root() / _workspace_id(root)


def workspace_path(root: Path, *parts: str) -> Path:
    return workspace_data_dir(root).joinpath(*parts)
