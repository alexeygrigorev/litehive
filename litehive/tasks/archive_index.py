"""Archive index helpers that do not depend on task storage."""

from __future__ import annotations

import csv
import re
from pathlib import Path

from litehive.tasks.paths import tasks_root

_ARCHIVED_TASK_DIR_RE = re.compile(r"^(T-\d{4})-")


def archive_root(root: Path) -> Path:
    return tasks_root(root, bootstrap=False) / "archive"


def archive_index_path(root: Path) -> Path:
    return archive_root(root) / "INDEX.csv"


def archived_task_ids(root: Path) -> set[str]:
    ids: set[str] = set()
    index_path = archive_index_path(root)
    if index_path.exists():
        with index_path.open(newline="", encoding="utf-8") as handle:
            ids.update(row["id"] for row in csv.DictReader(handle) if row.get("id"))

    archive_dir = archive_root(root)
    if archive_dir.exists():
        for path in archive_dir.iterdir():
            if not path.is_dir():
                continue
            match = _ARCHIVED_TASK_DIR_RE.match(path.name)
            if match is not None:
                ids.add(match.group(1))
    return ids
