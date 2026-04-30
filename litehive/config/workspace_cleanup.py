"""Cleanup helpers for deprecated workspace-owned YAML artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import tarfile

from litehive.config.paths import workspace_path
from litehive.config.workspace_files import config_path, workspace_dir
from litehive.state.backup import create_workspace_backup


@dataclass(frozen=True)
class WorkspaceYamlCleanup:
    removed_paths: tuple[Path, ...]
    archive_path: Path | None
    database_backup_path: Path | None


def find_deprecated_workspace_yaml(root: Path) -> list[Path]:
    base = workspace_dir(root)
    if not base.exists():
        return []
    allowed_config = config_path(root).resolve()
    candidates = [
        path
        for pattern in ("*.yaml", "*.yml")
        for path in base.rglob(pattern)
        if path.is_file() and path.resolve() != allowed_config
    ]
    return sorted(set(candidates))


def cleanup_deprecated_workspace_yaml(root: Path) -> WorkspaceYamlCleanup:
    paths = find_deprecated_workspace_yaml(root)
    if not paths:
        return WorkspaceYamlCleanup(removed_paths=(), archive_path=None, database_backup_path=None)

    database_backup = create_workspace_backup(root)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = workspace_path(root, "legacy-yaml-backups")
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"workspace-yaml-{timestamp}.tar.gz"
    base = workspace_dir(root)

    with tarfile.open(archive_path, "w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=path.relative_to(base))

    removed: list[Path] = []
    for path in paths:
        path.unlink(missing_ok=True)
        removed.append(path)
        _remove_empty_parents(path.parent, stop_at=base)

    return WorkspaceYamlCleanup(
        removed_paths=tuple(removed),
        archive_path=archive_path,
        database_backup_path=database_backup.path,
    )


def _remove_empty_parents(path: Path, *, stop_at: Path) -> None:
    while path != stop_at and path.is_relative_to(stop_at):
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent
