"""Task activity boundary over the SQLite-backed store plus comments.yaml mirroring."""

from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Iterable

from pydantic import ValidationError
import yaml

from litehive.db.schema import connect_workspace_db
from litehive.domain.reports import TaskActivityEntry
from litehive.domain.task import TaskRecord
from .paths import task_dir, tasks_root

_TASK_DIR_ID_PATTERN = re.compile(r"^(T-\d{4})-")


def task_activity_path(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "comments.yaml"


def legacy_task_activity_path(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "thread.yaml"


def resolve_task_activity_path(root: Path, task: TaskRecord) -> Path:
    comments_path = task_activity_path(root, task)
    if comments_path.exists():
        return comments_path
    legacy_path = legacy_task_activity_path(root, task)
    if legacy_path.exists():
        return legacy_path
    return comments_path


def load_task_activity(root: Path, task: TaskRecord) -> list[TaskActivityEntry]:
    comments_payload = _read_task_activity_file(task_activity_path(root, task))
    if comments_payload is not None:
        if comments_payload[1]:
            return comments_payload[0]
        return _load_task_activity_from_db(root, task)
    legacy_payload = _read_task_activity_file(legacy_task_activity_path(root, task))
    if legacy_payload is not None:
        if legacy_payload[1]:
            return legacy_payload[0]
        return _load_task_activity_from_db(root, task)
    return _load_task_activity_from_db(root, task)


def _load_task_activity_from_db(root: Path, task: TaskRecord) -> list[TaskActivityEntry]:
    with connect_workspace_db(root) as connection:
        rows = connection.execute(
            """
            SELECT payload
            FROM task_activity
            WHERE task_id = ?
            ORDER BY entry_index
            """,
            (task.id,),
        ).fetchall()

    activity: list[TaskActivityEntry] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            activity.append(TaskActivityEntry(**payload))
        except ValidationError:
            continue
    return activity


def _read_task_activity_file(path: Path) -> tuple[list[TaskActivityEntry], bool] | None:
    if not path.exists():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return [], False
    return _deserialize_task_activity_payload(loaded)


def _deserialize_task_activity_payload(payload: object) -> tuple[list[TaskActivityEntry], bool]:
    if payload is None:
        return [], True
    if not isinstance(payload, list):
        return [], False
    activity: list[TaskActivityEntry] = []
    valid = True
    for entry in payload:
        if not isinstance(entry, dict):
            valid = False
            continue
        try:
            activity.append(TaskActivityEntry(**entry))
        except ValidationError:
            valid = False
    return activity, valid


def _write_task_activity_file(path: Path, activity: list[TaskActivityEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump([entry.model_dump(mode="json") for entry in activity], sort_keys=False),
        encoding="utf-8",
    )


def _save_task_activity_to_db(root: Path, task_id: str, activity: list[TaskActivityEntry]) -> None:
    with connect_workspace_db(root) as connection:
        connection.execute("DELETE FROM task_activity WHERE task_id = ?", (task_id,))
        for entry_index, entry in enumerate(activity):
            payload = json.dumps(entry.model_dump(mode="json"))
            connection.execute(
                """
                INSERT INTO task_activity (task_id, entry_index, created_at, payload)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, entry_index, entry.created_at, payload),
            )
        connection.commit()


def save_task_activity(root: Path, task: TaskRecord, activity: list[TaskActivityEntry]) -> None:
    _save_task_activity_to_db(root, task.id, activity)
    _write_task_activity_file(task_activity_path(root, task), activity)
    legacy_path = legacy_task_activity_path(root, task)
    if legacy_path.exists():
        legacy_path.unlink()


def append_task_activity(root: Path, task: TaskRecord, entry: TaskActivityEntry) -> None:
    activity = load_task_activity(root, task)
    activity.append(entry)
    save_task_activity(root, task, activity)


def latest_task_activity_entry(
    root: Path,
    task: TaskRecord,
    *,
    role: str | None = None,
    stage: str | None = None,
    verdicts: Iterable[str] | None = None,
    after: datetime | None = None,
) -> TaskActivityEntry | None:
    allowed_verdicts = None if verdicts is None else set(verdicts)
    for entry in reversed(load_task_activity(root, task)):
        if role is not None and entry.role != role:
            continue
        if stage is not None and entry.stage != stage:
            continue
        entry_verdict = str(entry.verdict)
        if allowed_verdicts is not None and entry_verdict not in allowed_verdicts:
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


def migrate_legacy_task_activity_files(root: Path) -> bool:
    tasks_dir = tasks_root(root, bootstrap=False)
    if not tasks_dir.exists():
        return False

    mutated = False
    legacy_paths = sorted(tasks_dir.glob("*/thread.yaml"))
    archive_dir = tasks_dir / "archive"
    if archive_dir.exists():
        legacy_paths.extend(sorted(archive_dir.glob("*/thread.yaml")))

    for legacy_path in legacy_paths:
        if not legacy_path.is_file():
            continue
        comments_path = legacy_path.with_name("comments.yaml")
        if comments_path.exists():
            legacy_path.unlink()
            mutated = True
            continue

        legacy_path.replace(comments_path)
        mutated = True

        task_id = _task_id_from_task_dir_name(comments_path.parent.name)
        if task_id is None:
            continue

        parsed = _read_task_activity_file(comments_path)
        if parsed is None or not parsed[1]:
            continue
        _save_task_activity_to_db(root, task_id, parsed[0])

    return mutated


def _task_id_from_task_dir_name(name: str) -> str | None:
    match = _TASK_DIR_ID_PATTERN.match(name)
    if match is None:
        return None
    return match.group(1)
