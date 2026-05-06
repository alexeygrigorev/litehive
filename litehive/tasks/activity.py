"""Task activity boundary over the SQLite-backed store."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Iterable

from pydantic import ValidationError

from litehive.domain.agent import SubagentId
from litehive.domain.common import Verdict
from litehive.domain.reports import TaskActivityEntry, TaskActivityStage
from litehive.domain.task import TaskRecord
from litehive.tasks.event_log import append_task_event
from litehive.workspace import Workspace


@dataclass(frozen=True, slots=True)
class TaskActivityLog:
    """
    Workspace-scoped activity feed for one task.

    Owns query operations that need both the persisted task activity
    rows and the task identity. Callers that already hold a
    ``Workspace`` should get one through ``workspace.task_activity``
    instead of passing both objects to loose query helpers.
    """

    workspace: Workspace
    task: TaskRecord

    def latest_entry(
        self,
        role: str | None = None,
        stage: TaskActivityStage | None = None,
        source_subagent_id: SubagentId | None = None,
        verdicts: Iterable[str | Verdict] | None = None,
        after: datetime | None = None,
    ) -> TaskActivityEntry | None:
        """
        Find the most recent persisted activity entry matching the filters.

        Used by post-turn readers that need the verdict submitted through
        ``litehive agent report`` for a specific task, stage, and
        subagent session.
        """
        if verdicts is None:
            allowed_verdicts = None
        else:
            allowed_verdicts = {str(verdict) for verdict in verdicts}
        for entry in reversed(load_task_activity(self.workspace, self.task)):
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


def load_task_activity(workspace: Workspace, task: TaskRecord) -> list[TaskActivityEntry]:
    """
    Read the persisted activity feed (agent verdicts and reports) for a task.

    Malformed rows are skipped rather than raised so a single corrupt entry
    cannot blank out the whole feed for the lifecycle code that consumes it
    (stage builders, retraction logic, the task-logs renderer).
    """
    with workspace.connect() as connection:
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


def _save_task_activity_to_db(workspace: Workspace, task_id: str, activity: list[TaskActivityEntry]) -> None:
    """
    Replace ``task_activity`` rows and emit a ``task_reported`` event atomically.

    The single low-level write used by both ``save_task_activity`` and
    ``append_task_activity``; deleting and reinserting the whole feed is
    simpler than tracking per-row diffs and the activity feed is small
    enough that the cost is negligible.
    """
    with workspace.connect() as connection:
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
            workspace,
            event_type="task_reported",
            task_id=task_id,
            payload={"activity": [entry.model_dump(mode="json") for entry in activity]},
        )
        connection.commit()


def save_task_activity(workspace: Workspace, task: TaskRecord, activity: list[TaskActivityEntry]) -> None:
    """
    Replace the task's activity feed wholesale.

    Used by the retraction path that needs to rewrite an existing entry's
    message in place (e.g. marking a pass entry as retracted after the
    requeue-time filesystem check) rather than appending a new entry.
    """
    _save_task_activity_to_db(workspace, task.id, activity)


def append_task_activity(workspace: Workspace, task: TaskRecord, entry: TaskActivityEntry) -> None:
    """
    Append one verdict or report entry; the common write path for activity.

    Pays the load+rewrite cost on every append so the on-disk ordering tracks
    arrival order without requiring callers to track entry indexes themselves.
    Reached by every stage controller and by the recovery report path.
    """
    activity = load_task_activity(workspace, task)
    activity.append(entry)
    save_task_activity(workspace, task, activity)


def _parse_created_at(value: str) -> datetime:
    """
    Parse the ``created_at`` ISO string into a UTC-aware datetime.

    Normalises trailing ``Z`` to ``+00:00`` and naive datetimes to UTC so
    ``TaskActivityLog.latest_entry``'s ``after`` filter can compare entries
    written by older code (no timezone) against newer UTC-aware writers
    without raising on the mixed-shape comparison.
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
