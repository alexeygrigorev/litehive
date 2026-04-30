"""Archive and cleanup for completed tasks moved into history."""

from datetime import datetime, timezone
import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from litehive.domain.common import utcnow
from litehive.domain.task import TaskRecord
from litehive.fs_cleanup import remove_tree_logged

from litehive.state.records import get_task_record, list_tasks, task_state_for_storage
from litehive.state.locking import workspace_lock
from litehive.state.persist import load_state, save_state_without_runner_guard
from litehive.state.store import runtime_store
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.queue import drop_task_from_workspace_state
from litehive.tasks.archive_index import (
    archive_index_path,
    archive_root as archive_index_root,
)
from .paths import task_dir
from litehive.state.persist import atomic_write_text

logger = logging.getLogger(__name__)

_ARCHIVABLE_TASK_STATUSES = {"done", "closed"}
_CLOSE_REASON_INDEX_STATUS = {"execution_cancelled": "cancelled"}


def archive_root(root: Path) -> Path:
    return archive_index_root(root)


def _archive_index_path(root: Path) -> Path:
    return archive_index_path(root)


def _archive_index_status(task: TaskRecord) -> str:
    if str(task.status) == "archived":
        reason = task.close_reason or task.runtime.pipeline.last_outcome.reason_code
        if reason:
            return _CLOSE_REASON_INDEX_STATUS.get(str(reason), str(reason))
        if str(task.pipeline_status) == "done":
            return "done"
        return "archived"
    if str(task.status) == "closed":
        close_reason = task.close_reason or task.runtime.pipeline.last_outcome.reason_code
        return _CLOSE_REASON_INDEX_STATUS.get(str(close_reason), str(close_reason or "closed"))
    return _CLOSE_REASON_INDEX_STATUS.get(str(task.status), str(task.status))


def _write_archive_index(root: Path, tasks: list[TaskRecord]) -> None:
    """Write the archive index from the current archived task set."""
    index_path = _archive_index_path(root)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["id,title,status,created"]
    for task in tasks:
        created = str(task.created_at)[:10] if task.created_at else ""
        title = (task.title or "").replace('"', '""')
        lines.append(f'{task.id},"{title}",{_archive_index_status(task)},{created}')

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
        lines.append(f'{task.id},"{title}",{_archive_index_status(task)},{created}')

    atomic_write_text(index_path, "\n".join(lines) + "\n")


def _archive_task_path(root: Path, task_id: str) -> Path | None:
    archive = archive_root(root)
    if not archive.exists():
        return None
    matches = sorted(path for path in archive.glob(f"{task_id}-*") if path.is_dir())
    return matches[0] if matches else None


def _archived_at_for_tombstone(task: TaskRecord) -> str:
    return task.updated_at or utcnow()


def _hard_delete_archived_task(
    root: Path,
    task: TaskRecord,
    *,
    state,
    deletion_reason: str,
    audit_actor: str,
    audit_source: str,
    extra_context: dict[str, Any] | None = None,
) -> TaskRecord:
    queue_before = list(state.queue)
    drop_task_from_workspace_state(state, task.id)
    archive_dir = _archive_task_path(root, task.id)
    deleted_at = utcnow()
    if archive_dir is not None:
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
                    "archived_at": _archived_at_for_tombstone(task),
                    "deleted_at": deleted_at,
                    "deletion_reason": deletion_reason,
                    "archive_path": "" if archive_dir is None else str(archive_dir.relative_to(root)),
                    **dict(extra_context or {}),
                },
            )
        ],
    )
    return task


def _validate_archivable_tasks(root: Path, task_ids: list[str]) -> list[TaskRecord]:
    if not task_ids:
        raise ValueError("At least one task id is required")
    seen: set[str] = set()
    tasks: list[TaskRecord] = []
    for task_id in task_ids:
        normalized_task_id = task_id.strip()
        if not normalized_task_id:
            raise ValueError("Task id must not be empty")
        if normalized_task_id in seen:
            raise ValueError(f"Duplicate task id: {normalized_task_id}")
        seen.add(normalized_task_id)
        task = get_task_record(root, normalized_task_id, include_archived=True)
        if task is None:
            raise ValueError(f"Task {normalized_task_id} not found")
        if task.status == "archived":
            raise ValueError(f"Task {task.id} is already archived")
        if str(task.status) not in _ARCHIVABLE_TASK_STATUSES:
            raise ValueError(
                f"Task {task.id} has status '{task.status}' — only done or closed terminal tasks can be archived"
            )
        src = task_dir(root, task)
        dst = archive_root(root) / src.name
        if dst.exists():
            raise ValueError(f"Archive destination already exists: {dst.name}")
        tasks.append(task)
    return tasks


def _archive_validated_task(
    root: Path,
    task: TaskRecord,
    *,
    state,
    queue_before: list[str],
    audit_actor: str,
    audit_source: str,
) -> TaskRecord:
    from litehive.tasks.duplicates import refresh_duplicate_task_index_if_initialized

    before_task = snapshot_task_audit_state(task)
    src = task_dir(root, task)
    dst = archive_root(root) / src.name
    now = utcnow()
    task.updated_at = now
    if task.close_reason is None and str(task.status) == "done":
        task.close_reason = "done"
    task.status = "archived"
    if state.active_task_id == task.id:
        state.active_task_id = None
    state.queue = [queued_id for queued_id in state.queue if queued_id != task.id]
    audit_entry = build_task_audit_entry(
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
    runtime_store(root).save_runtime_transaction(
        task_intents={task.id: task.to_intent_record()},
        task_states={task.id: task_state_for_storage(task)},
        workspace_state=state,
        audit_entries=[audit_entry],
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.move(str(src), str(dst))
    else:
        dst.mkdir(parents=True, exist_ok=True)
    _update_archive_index(root, [task])
    refresh_duplicate_task_index_if_initialized(root)
    return task


def archive_task(
    root: Path,
    task_id: str,
    *,
    audit_actor: str = "operator",
    audit_source: str = "archive",
) -> TaskRecord:
    """Move a single done or closed terminal task to the archive directory."""

    with workspace_lock(root):
        task = _validate_archivable_tasks(root, [task_id])[0]
        state = load_state(root)
        queue_before = list(state.queue)
        return _archive_validated_task(
            root,
            task,
            state=state,
            queue_before=queue_before,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )


def archive_tasks(
    root: Path,
    task_ids: list[str],
    *,
    audit_actor: str = "operator",
    audit_source: str = "archive",
) -> list[TaskRecord]:
    """Move explicit done or closed terminal tasks to the archive directory."""
    with workspace_lock(root):
        tasks = _validate_archivable_tasks(root, task_ids)
        state = load_state(root)
        archived: list[TaskRecord] = []
        for task in tasks:
            queue_before = list(state.queue)
            archived.append(
                _archive_validated_task(
                    root,
                    task,
                    state=state,
                    queue_before=queue_before,
                    audit_actor=audit_actor,
                    audit_source=audit_source,
                )
            )
        return archived


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
                archived_task = archive_task(root, task.id)
            except (ValueError, FileNotFoundError) as exc:
                if on_skip is not None:
                    on_skip(task.id, exc)
                continue
            archived.append(archived_task)
    return archived


def list_archived_tasks(root: Path) -> list[TaskRecord]:
    """List tasks moved into the archive."""
    return [
        task
        for task in list_tasks(root, include_runtime=True, strict=False, include_archived=True)
        if task.status == "archived"
    ]


def get_archived_task(root: Path, task_id: str) -> TaskRecord | None:
    """Return an archived task record by id, or None if it is not archived."""
    task = get_task_record(root, task_id, include_archived=True)
    if task is None:
        return None
    if task.status != "archived":
        return None
    return task


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
        task = get_archived_task(root, task_id)
        if task is None:
            live_task = get_task_record(root, task_id)
            if live_task is not None:
                raise ValueError(f"Task {task_id} has status '{live_task.status}' — only archived tasks can be deleted")
            raise ValueError(f"Task {task_id} not found")
        state = load_state(root)
        task = _hard_delete_archived_task(
            root,
            task,
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
    deleted: list[TaskRecord] = []
    with workspace_lock(root):
        state = load_state(root)
        for task in list_archived_tasks(root):
            archived_at = task.updated_at
            try:
                archived_dt = datetime.fromisoformat(archived_at)
            except (TypeError, ValueError):
                continue
            age_seconds = (now - archived_dt).total_seconds()
            if age_seconds >= max_age_seconds:
                deleted.append(
                    _hard_delete_archived_task(
                        root,
                        task,
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
