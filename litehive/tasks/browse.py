"""Browse recently created tasks using persisted task metadata."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from litehive.domain.task import TaskCreationSource, TaskRecord
from litehive.state.records import list_tasks
from litehive.tasks.archive import list_archived_tasks
from litehive.tasks.recent import parse_compact_duration_seconds


@dataclass(frozen=True)
class CreatedTaskBrowseRow:
    task_id: str
    title: str
    created_at: str
    source: str
    context: str


def list_recently_created_tasks(
    root: Path,
    *,
    since: str = "24h",
    now: datetime | None = None,
) -> list[CreatedTaskBrowseRow]:
    window_seconds = parse_compact_duration_seconds(since)
    cutoff = _coerce_utc(now) - timedelta(seconds=window_seconds)
    tasks = _load_task_records(root)
    title_by_id = {task.id: task.title for task in tasks}

    recent: list[tuple[datetime, CreatedTaskBrowseRow]] = []
    for task in tasks:
        created_at = _parse_created_at(task.created_at)
        if created_at is None or created_at < cutoff:
            continue
        recent.append(
            (
                created_at,
                CreatedTaskBrowseRow(
                    task_id=task.id,
                    title=task.title,
                    created_at=task.created_at,
                    source=_creation_source(task.created_from),
                    context=_creation_context(task.created_from, title_by_id=title_by_id),
                ),
            )
        )

    recent.sort(key=lambda item: item[1].task_id)
    recent.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in recent]


def _load_task_records(root: Path) -> list[TaskRecord]:
    task_by_id: dict[str, TaskRecord] = {}
    for task in list_archived_tasks(root):
        task_by_id[task.id] = task
    for task in list_tasks(root, include_runtime=False, strict=False):
        task_by_id[task.id] = task
    return list(task_by_id.values())


def _creation_source(created_from: TaskCreationSource | None) -> str:
    if created_from is None:
        return "-"
    return created_from.source


def _creation_context(
    created_from: TaskCreationSource | None,
    *,
    title_by_id: dict[str, str],
) -> str:
    if created_from is None:
        return "-"

    parts: list[str] = []
    if created_from.task_id:
        task_context = created_from.task_id
        parent_title = title_by_id.get(created_from.task_id)
        if parent_title:
            task_context = f"{task_context} {parent_title}"
        parts.append(task_context)
    if created_from.stage:
        parts.append(f"stage={created_from.stage}")
    if created_from.role:
        parts.append(f"role={created_from.role}")
    if created_from.source == "follow_up":
        parts.append(f"blocking={'yes' if created_from.blocking else 'no'}")
    return "; ".join(parts) or "-"


def _coerce_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC).replace(microsecond=0)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC).replace(microsecond=0)
    return now.astimezone(UTC).replace(microsecond=0)


def _parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
