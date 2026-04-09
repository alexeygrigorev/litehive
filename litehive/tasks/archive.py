"""Archive and cleanup for done tasks."""

import re
import shutil
from collections.abc import Callable
from pathlib import Path

import yaml

from litehive.models import TaskRecord, utcnow

from .crud import _load_task_record_file, list_tasks, require_task
from .locking import _workspace_lock
from .paths import task_dir, tasks_root
from .persistence import _atomic_write_text


def archive_root(root: Path) -> Path:
    return tasks_root(root) / "archive"


def _archive_index_path(root: Path) -> Path:
    return archive_root(root) / "INDEX.csv"


def _update_archive_index(root: Path, tasks: list[TaskRecord]) -> None:
    """Append newly archived tasks to INDEX.csv (or create it)."""
    index_path = _archive_index_path(root)
    if index_path.exists():
        existing = index_path.read_text(encoding="utf-8")
    else:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        existing = "id,title,status,created"

    lines = existing.rstrip("\n").splitlines()
    for task in tasks:
        created = str(task.created_at)[:10] if task.created_at else ""
        title = (task.title or "").replace('"', '""')
        lines.append(f'{task.id},"{title}",{task.status},{created}')

    _atomic_write_text(index_path, "\n".join(lines) + "\n")


def archive_task(root: Path, task_id: str) -> TaskRecord:
    """Move a single done task to the archive directory."""
    with _workspace_lock(root):
        task = require_task(root, task_id)
        if task.status != "done":
            raise ValueError(
                f"Task {task.id} has status '{task.status}' — only done tasks can be archived"
            )
        src = task_dir(root, task)
        dst = archive_root(root) / src.name
        if dst.exists():
            raise ValueError(f"Archive destination already exists: {dst.name}")
        now = utcnow()
        task.updated_at = now
        # Write archive timestamp into task.yaml before moving
        task_yaml_path = src / "task.yaml"
        data = yaml.safe_load(task_yaml_path.read_text(encoding="utf-8")) or {}
        data["archived_at"] = now
        data["updated_at"] = now
        _atomic_write_text(task_yaml_path, yaml.safe_dump(data, sort_keys=False))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        _update_archive_index(root, [task])
        return task


def archive_done_tasks(
    root: Path,
    on_skip: Callable[[str, Exception], None] | None = None,
) -> list[TaskRecord]:
    """Move all done tasks to the archive directory."""
    tasks = list_tasks(root, include_runtime=False)
    archived: list[TaskRecord] = []
    for task in tasks:
        if task.status == "done" and task.pipeline_status == "done":
            try:
                archive_task(root, task.id)
            except (ValueError, FileNotFoundError) as exc:
                if on_skip is not None:
                    on_skip(task.id, exc)
                continue
            archived.append(task)
    return archived


def list_archived_tasks(root: Path) -> list[TaskRecord]:
    """List tasks in the archive directory."""
    archive = archive_root(root)
    if not archive.exists():
        return []
    records: list[TaskRecord] = []
    for child in sorted(archive.iterdir()):
        if not child.is_dir():
            continue
        path = child / "task.yaml"
        if not path.exists():
            continue
        records.append(_load_task_record_file(path))
    return records


def _parse_duration(duration_str: str) -> int:
    """Parse a duration string like '30d' into seconds."""
    match = re.match(r"^(\d+)([dhms])$", duration_str.strip())
    if not match:
        raise ValueError(
            f"Invalid duration format '{duration_str}'. Use <number><unit> where unit is d/h/m/s (e.g. 30d)"
        )
    value = int(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]


def cleanup_archived_tasks(root: Path, older_than: str) -> list[TaskRecord]:
    """Delete archived tasks older than the given duration."""
    from datetime import datetime, timezone

    max_age_seconds = _parse_duration(older_than)
    now = datetime.now(timezone.utc)
    archive = archive_root(root)
    if not archive.exists():
        return []
    deleted: list[TaskRecord] = []
    for child in sorted(archive.iterdir()):
        if not child.is_dir():
            continue
        path = child / "task.yaml"
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        archived_at = data.get("archived_at")
        if archived_at is None:
            continue
        try:
            archived_dt = datetime.fromisoformat(archived_at)
        except (TypeError, ValueError):
            continue
        age_seconds = (now - archived_dt).total_seconds()
        if age_seconds >= max_age_seconds:
            task = _load_task_record_file(path)
            shutil.rmtree(child)
            deleted.append(task)
    return deleted
