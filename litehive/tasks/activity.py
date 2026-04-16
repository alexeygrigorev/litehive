"""Task activity boundary over the current task activity store."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import yaml

from litehive.domain.reports import TaskThreadComment
from litehive.domain.task import TaskRecord

from .paths import task_dir


def task_activity_path(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "comments.yaml"


def load_task_activity(root: Path, task: TaskRecord) -> list[TaskThreadComment]:
    path = task_activity_path(root, task)
    if not path.exists():
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        return []
    activity: list[TaskThreadComment] = []
    for entry in loaded:
        if not isinstance(entry, dict):
            continue
        normalized = dict(entry)
        if "stage" not in normalized and "step" in normalized:
            normalized["stage"] = normalized.pop("step")
        activity.append(TaskThreadComment(**normalized))
    return activity


def save_task_activity(root: Path, task: TaskRecord, activity: list[TaskThreadComment]) -> None:
    path = task_activity_path(root, task)
    path.write_text(
        yaml.safe_dump([entry.model_dump(mode="json") for entry in activity], sort_keys=False),
        encoding="utf-8",
    )


def append_task_activity(root: Path, task: TaskRecord, entry: TaskThreadComment) -> None:
    activity = load_task_activity(root, task)
    activity.append(entry)
    save_task_activity(root, task, activity)


def latest_task_activity_entry(
    root: Path,
    task: TaskRecord,
    *,
    role: str | None = None,
    stage: str | None = None,
    step: str | None = None,
    verdicts: Iterable[str] | None = None,
    after: datetime | None = None,
) -> TaskThreadComment | None:
    stage = stage or step
    allowed_verdicts = None if verdicts is None else set(verdicts)
    for entry in reversed(load_task_activity(root, task)):
        if role is not None and entry.role != role:
            continue
        if stage is not None and entry.stage != stage:
            continue
        if allowed_verdicts is not None and entry.verdict not in allowed_verdicts:
            continue
        if after is not None and _parse_created_at(entry.created_at) <= after:
            continue
        return entry
    return None


def _parse_created_at(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
