"""Task activity boundary over the SQLite-backed activity store."""

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from pydantic import ValidationError
<<<<<<< HEAD
=======
import yaml
>>>>>>> 61b0a5fa (litehive T-0398: auto-commit worktree changes)

from litehive.db.schema import connect_workspace_db
from litehive.domain.reports import TaskThreadComment
from litehive.domain.task import TaskRecord
from .paths import task_dir


def task_activity_path(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "comments.yaml"


def _normalize_activity_entry(entry: dict[str, object]) -> TaskThreadComment | None:
    normalized = dict(entry)
    if "stage" not in normalized and "step" in normalized:
        normalized["stage"] = normalized.pop("step")
    try:
        return TaskThreadComment(**normalized)
    except ValidationError:
        return None


def _load_task_activity_from_db(root: Path, task: TaskRecord) -> tuple[bool, list[TaskThreadComment]]:
    try:
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
    except sqlite3.OperationalError:
        return False, []

    activity: list[TaskThreadComment] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        comment = _normalize_activity_entry(payload)
        if comment is not None:
            activity.append(comment)
    return bool(rows), activity


def load_task_activity(root: Path, task: TaskRecord) -> list[TaskThreadComment]:
<<<<<<< HEAD
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

=======
    rows_found, activity = _load_task_activity_from_db(root, task)
    if rows_found:
        return activity

    path = task_activity_path(root, task)
    if not path.exists():
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        return []
>>>>>>> 61b0a5fa (litehive T-0398: auto-commit worktree changes)
    activity: list[TaskThreadComment] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
        except json.JSONDecodeError:
            continue
<<<<<<< HEAD
        if not isinstance(payload, dict):
            continue
        normalized = dict(payload)
        if "stage" not in normalized and "step" in normalized:
            normalized["stage"] = normalized.pop("step")
        try:
            activity.append(TaskThreadComment(**normalized))
        except ValidationError:
            continue
=======
        comment = _normalize_activity_entry(entry)
        if comment is not None:
            activity.append(comment)
>>>>>>> 61b0a5fa (litehive T-0398: auto-commit worktree changes)
    return activity


def save_task_activity(root: Path, task: TaskRecord, activity: list[TaskThreadComment]) -> None:
<<<<<<< HEAD
    with connect_workspace_db(root) as connection:
        connection.execute("DELETE FROM task_activity WHERE task_id = ?", (task.id,))
        for entry_index, entry in enumerate(activity):
            payload = json.dumps(entry.model_dump(mode="json"))
            connection.execute(
                """
                INSERT INTO task_activity (task_id, entry_index, created_at, payload)
                VALUES (?, ?, ?, ?)
                """,
                (task.id, entry_index, entry.created_at, payload),
            )
=======
    path = task_activity_path(root, task)
    path.write_text(
        yaml.safe_dump([entry.model_dump(mode="json") for entry in activity], sort_keys=False),
        encoding="utf-8",
    )
    with connect_workspace_db(root) as connection:
        connection.execute("DELETE FROM task_activity WHERE task_id = ?", (task.id,))
        connection.executemany(
            """
            INSERT INTO task_activity (task_id, entry_index, created_at, payload)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    task.id,
                    index,
                    entry.created_at,
                    json.dumps(entry.model_dump(mode="json"), sort_keys=True),
                )
                for index, entry in enumerate(activity)
            ],
        )
>>>>>>> 61b0a5fa (litehive T-0398: auto-commit worktree changes)
        connection.commit()


def append_task_activity(root: Path, task: TaskRecord, entry: TaskThreadComment) -> None:
    payload = json.dumps(entry.model_dump(mode="json"))
    with connect_workspace_db(root) as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(entry_index), -1) AS max_index FROM task_activity WHERE task_id = ?",
            (task.id,),
        ).fetchone()
        next_index = int(row["max_index"]) + 1
        connection.execute(
            """
            INSERT INTO task_activity (task_id, entry_index, created_at, payload)
            VALUES (?, ?, ?, ?)
            """,
            (task.id, next_index, entry.created_at, payload),
        )
        connection.commit()


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
        entry_verdict = entry.verdict.value if hasattr(entry.verdict, "value") else str(entry.verdict)
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
