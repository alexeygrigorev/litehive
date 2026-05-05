"""Task activity boundary over the SQLite-backed store."""

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from litehive.db.schema import connect_workspace_db
from litehive.domain.reports import TaskActivityEntry
from litehive.domain.task import TaskRecord
from litehive.tasks.event_log import append_task_event
from litehive.workspace import Workspace


def load_task_activity(root: Path, task: TaskRecord) -> list[TaskActivityEntry]:
    """Read the persisted activity feed (agent verdicts and reports) for a task; tolerates malformed rows by skipping them so a single corrupt entry does not blank out the whole feed for the lifecycle code."""
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
        append_task_event(
            Workspace.from_path(root),
            event_type="task_reported",
            task_id=task_id,
            payload={"activity": [entry.model_dump(mode="json") for entry in activity]},
        )
        connection.commit()


def save_task_activity(root: Path, task: TaskRecord, activity: list[TaskActivityEntry]) -> None:
    """Replace the task's activity feed wholesale; used by the retraction path that needs to rewrite an existing entry's message in place rather than appending a new one."""
    _save_task_activity_to_db(root, task.id, activity)


def append_task_activity(root: Path, task: TaskRecord, entry: TaskActivityEntry) -> None:
    """Append one verdict/report entry, the common write path; takes the load+rewrite cost so the on-disk ordering matches arrival order without requiring callers to track entry indexes."""
    activity = load_task_activity(root, task)
    activity.append(entry)
    save_task_activity(root, task, activity)


def latest_task_activity_entry(
    root: Path,
    task: TaskRecord,
    role: str | None = None,
    stage: str | None = None,
    source_subagent_id: str | None = None,
    verdicts: Iterable[str] | None = None,
    after: datetime | None = None,
) -> TaskActivityEntry | None:
    """Find the most recent activity entry matching the given filter; the stage-report builder uses this to locate the verdict an agent submitted via ``litehive agent report`` for the just-finished subagent run."""
    if verdicts is None:
        allowed_verdicts = None
    else:
        allowed_verdicts = set(verdicts)
    for entry in reversed(load_task_activity(root, task)):
        if role is not None and entry.role != role:
            continue
        if stage is not None and entry.stage != stage:
            continue
        if source_subagent_id is not None and entry.source_subagent_id != source_subagent_id:
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
