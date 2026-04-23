"""Archive and cleanup for completed tasks moved into history."""

from datetime import datetime, timezone
import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from litehive.domain.common import utcnow
from litehive.domain.task import TaskRecord
from litehive.fs_cleanup import remove_tree_logged

from litehive.state.records import get_task_record, list_tasks, require_task, task_state_for_storage
from litehive.state.locking import workspace_lock
from litehive.state.persist import load_state, save_state_without_runner_guard
from litehive.state.store import runtime_store
from litehive.tasks.audit import append_task_audit_entries, build_task_audit_entry, snapshot_task_audit_state
from .paths import task_dir, tasks_root
from litehive.state.persist import atomic_write_text

logger = logging.getLogger(__name__)


def archive_root(root: Path) -> Path:
    return tasks_root(root) / "archive"


def _load_archived_task_payload(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Archived task file must contain a mapping: {path}")
    return dict(data)


def _load_archived_task_record(path: Path) -> TaskRecord:
    data = _load_archived_task_payload(path)
    # Legacy archived task.yaml files predate persisted archived status fields.
    # Tasks under archive/ are history-only, so default missing status accordingly.
    data.setdefault("status", "archived")
    data.setdefault("pipeline_status", "done")
    data.setdefault("updated_at", data.get("archived_at") or utcnow())
    data.pop("archived_at", None)
    return TaskRecord(**data)


def _archive_index_path(root: Path) -> Path:
    return archive_root(root) / "INDEX.csv"


def _write_archive_index(root: Path, tasks: list[TaskRecord]) -> None:
    """Write the archive index from the current archived task set."""
    index_path = _archive_index_path(root)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["id,title,status,created"]
    for task in tasks:
        created = str(task.created_at)[:10] if task.created_at else ""
        title = (task.title or "").replace('"', '""')
        lines.append(f'{task.id},"{title}",{task.status},{created}')

    atomic_write_text(index_path, "\n".join(lines) + "\n")


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


def _archive_task_path(root: Path, task_id: str) -> Path | None:
    archive = archive_root(root)
    if not archive.exists():
        return None
    matches = sorted(archive.glob(f"{task_id}-*/task.yaml"))
    return matches[0] if matches else None


def _archived_at_for_tombstone(data: dict[str, Any]) -> str:
    for key in ("archived_at", "updated_at", "created_at"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return utcnow()


def _drop_task_from_workspace_state(state, task_id: str) -> bool:
    changed = False
    if state.active_task_id == task_id:
        state.active_task_id = None
        changed = True
    if task_id in state.queue:
        state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
        changed = True
    original_unmerged = len(state.unmerged_worktrees)
    state.unmerged_worktrees = [item for item in state.unmerged_worktrees if item.task_id != task_id]
    return changed or len(state.unmerged_worktrees) != original_unmerged


def _hard_delete_archived_task(
    root: Path,
    archive_task_yaml: Path,
    *,
    state,
    deletion_reason: str,
    audit_actor: str,
    audit_source: str,
    extra_context: dict[str, Any] | None = None,
) -> TaskRecord:
    task = _load_archived_task_record(archive_task_yaml)
    archived_payload = _load_archived_task_payload(archive_task_yaml)
    queue_before = list(state.queue)
    _drop_task_from_workspace_state(state, task.id)
    archive_dir = archive_task_yaml.parent
    deleted_at = utcnow()
    remove_tree_logged(
        archive_dir,
        logger=logger,
        target_label="archived task directory",
    )
    runtime_store(root).delete_task_records_preserving_audit(
        task.id,
        audit_entries=[
            build_task_audit_entry(
                task_id=task.id,
                created_at=deleted_at,
                action="deleted",
                actor=audit_actor,
                source=audit_source,
                before_task=task,
                after_task=None,
                before_queue=queue_before,
                after_queue=state.queue,
                context={
                    "title": task.title,
                    "archived_at": _archived_at_for_tombstone(archived_payload),
                    "deleted_at": deleted_at,
                    "deletion_reason": deletion_reason,
                    "archive_path": str(archive_dir.relative_to(root)),
                    **dict(extra_context or {}),
                },
            )
        ],
    )
    return task


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
        state = load_state(root)
        queue_before = list(state.queue)
        now = utcnow()
        task.status = "archived"
        task.updated_at = now
        if state.active_task_id == task.id:
            state.active_task_id = None
        state.queue = [queued_id for queued_id in state.queue if queued_id != task.id]
        # Write archive timestamp into task.yaml before moving
        task_yaml_path = src / "task.yaml"
        data = yaml.safe_load(task_yaml_path.read_text(encoding="utf-8")) or {}
        data["archived_at"] = now
        data["status"] = str(task.status)
        data["pipeline_status"] = str(task.pipeline_status)
        data["updated_at"] = now
        atomic_write_text(task_yaml_path, yaml.safe_dump(data, sort_keys=False))
        runtime_store(root).save_runtime_transaction(task_states={task.id: task_state_for_storage(task)})
        save_state_without_runner_guard(root, state)
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
                    before_queue=queue_before,
                    after_queue=state.queue,
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


def get_archived_task(root: Path, task_id: str) -> TaskRecord | None:
    """Return an archived task record by id, or None if it is not archived."""
    path = _archive_task_path(root, task_id)
    if path is None:
        return None
    return _load_archived_task_record(path)


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


def delete_archived_task(
    root: Path,
    task_id: str,
    *,
    reason: str,
    audit_actor: str = "operator",
    audit_source: str = "archive_delete",
) -> TaskRecord:
    from litehive.tasks.duplicates import refresh_duplicate_task_index_if_initialized

    reason = reason.strip()
    if not reason:
        raise ValueError("Delete reason must not be empty")

    with workspace_lock(root):
        archive_task_yaml = _archive_task_path(root, task_id)
        if archive_task_yaml is None:
            live_task = get_task_record(root, task_id)
            if live_task is not None:
                raise ValueError(
                    f"Task {task_id} has status '{live_task.status}' — only archived tasks can be deleted"
                )
            raise ValueError(f"Task {task_id} not found")
        state = load_state(root)
        task = _hard_delete_archived_task(
            root,
            archive_task_yaml,
            state=state,
            deletion_reason=reason,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )
        save_state_without_runner_guard(root, state)
        _write_archive_index(root, list_archived_tasks(root))
        refresh_duplicate_task_index_if_initialized(root)
        return task


def cleanup_archived_tasks(root: Path, older_than: str) -> list[TaskRecord]:
    """Delete archived tasks older than the given duration."""
    from litehive.tasks.duplicates import refresh_duplicate_task_index_if_initialized

    max_age_seconds = _parse_duration(older_than)
    now = datetime.now(timezone.utc)
    archive = archive_root(root)
    if not archive.exists():
        return []
    deleted: list[TaskRecord] = []
    with workspace_lock(root):
        state = load_state(root)
        for child in sorted(archive.iterdir()):
            if not child.is_dir():
                continue
            path = child / "task.yaml"
            if not path.exists():
                continue
            data = _load_archived_task_payload(path)
            archived_at = data.get("archived_at")
            if archived_at is None:
                continue
            try:
                archived_dt = datetime.fromisoformat(archived_at)
            except (TypeError, ValueError):
                continue
            age_seconds = (now - archived_dt).total_seconds()
            if age_seconds >= max_age_seconds:
                deleted.append(
                    _hard_delete_archived_task(
                        root,
                        path,
                        state=state,
                        deletion_reason=f"archive_cleanup older_than={older_than}",
                        audit_actor="system",
                        audit_source="archive_cleanup",
                        extra_context={"older_than": older_than},
                    )
                )
        if deleted:
            save_state_without_runner_guard(root, state)
            _write_archive_index(root, list_archived_tasks(root))
    if deleted:
        refresh_duplicate_task_index_if_initialized(root)
    return deleted
