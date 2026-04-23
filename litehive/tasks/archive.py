"""Archive and cleanup for done tasks."""

import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import yaml

from litehive.domain.common import utcnow
from litehive.domain.task import TaskRecord
from litehive.fs_cleanup import remove_tree_logged

from litehive.state.records import list_tasks, require_task
from litehive.state.locking import workspace_lock
from litehive.tasks.audit import append_task_audit_entries, build_task_audit_entry, snapshot_task_audit_state
from .paths import task_dir, tasks_root
from litehive.state.persist import atomic_write_text

logger = logging.getLogger(__name__)


def archive_root(root: Path) -> Path:
    return tasks_root(root) / "archive"


def _load_archived_task_record(path: Path) -> TaskRecord:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Archived task file must contain a mapping: {path}")
    data = dict(data)
    # Legacy archived task.yaml files predate persisted runtime fields.
    # Archived tasks are always done, so default missing fields accordingly.
    data.setdefault("status", "done")
    data.setdefault("pipeline_status", "done")
    data.pop("archived_at", None)
    data.pop("updated_at", None)
    return TaskRecord(**data)


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

    atomic_write_text(index_path, "\n".join(lines) + "\n")


def archive_task(
    root: Path,
    task_id: str,
    *,
    audit_actor: str = "operator",
    audit_source: str = "archive",
) -> TaskRecord:
    """Move a single done task to the archive directory."""
    from litehive.tasks.duplicates import refresh_duplicate_task_index_if_initialized

    with workspace_lock(root):
        task = require_task(root, task_id)
        before_task = snapshot_task_audit_state(task)
        if task.status != "done":
            raise ValueError(f"Task {task.id} has status '{task.status}' — only done tasks can be archived")
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
        data["status"] = str(task.status)
        data["pipeline_status"] = str(task.pipeline_status)
        data["updated_at"] = now
        atomic_write_text(task_yaml_path, yaml.safe_dump(data, sort_keys=False))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        _update_archive_index(root, [task])
        refresh_duplicate_task_index_if_initialized(root)
        append_task_audit_entries(
            root,
            [
                build_task_audit_entry(
                    task_id=task.id,
                    action="archived",
                    actor=audit_actor,
                    source=audit_source,
                    before_task=before_task,
                    after_task=task,
                    context={"archive_path": str(dst.relative_to(root))},
                )
            ],
        )
        return task


def archive_done_tasks(
    root: Path,
    on_skip: Callable[[str, Exception], None] | None = None,
) -> list[TaskRecord]:
    """Move all done tasks to the archive directory."""
    tasks = list_tasks(root, include_runtime=True, strict=False)
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
        records.append(_load_archived_task_record(path))
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
    from litehive.tasks.duplicates import refresh_duplicate_task_index_if_initialized

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
            task = _load_archived_task_record(path)
            remove_tree_logged(
                child,
                logger=logger,
                target_label="archived task directory",
            )
            append_task_audit_entries(
                root,
                [
                    build_task_audit_entry(
                        task_id=task.id,
                        action="removed",
                        actor="system",
                        source="archive_cleanup",
                        before_task=task,
                        after_task=None,
                        context={"older_than": older_than, "archive_path": str(child.relative_to(root))},
                    )
                ],
            )
            deleted.append(task)
    if deleted:
        refresh_duplicate_task_index_if_initialized(root)
    return deleted
