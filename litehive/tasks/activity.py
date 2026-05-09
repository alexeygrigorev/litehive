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
from litehive.tasks.event_log import TaskEventLog
from litehive.workspace import Workspace


@dataclass(frozen=True, slots=True)
class TaskActivityStore:
    """
    Workspace-scoped activity feed for one task.

    Owns query operations that need both the persisted task activity
    rows and the task identity. Callers that already hold a
    ``Workspace`` should get one through
    ``task_activity_store_for_task(workspace, task)`` instead of
    passing both objects to loose query helpers.
    """

    workspace: Workspace
    task: TaskRecord

    def load(self) -> list[TaskActivityEntry]:
        """
        Read this task's persisted activity feed.

        Malformed rows are skipped rather than raised so a single
        corrupt entry cannot blank out the whole feed for lifecycle
        code, prompt builders, or task-log renderers.
        """
        with self.workspace.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload
                FROM task_activity
                WHERE task_id = ?
                ORDER BY entry_index
                """,
                (self.task.id,),
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

    def save(self, activity: list[TaskActivityEntry]) -> None:
        """
        Replace this task's activity feed wholesale.

        Used by retraction paths that rewrite an existing entry in
        place rather than appending a new entry.
        """
        with self.workspace.connect() as connection:
            connection.execute("DELETE FROM task_activity WHERE task_id = ?", (self.task.id,))
            for entry_index, entry in enumerate(activity):
                payload = json.dumps(entry.model_dump(mode="json"))
                connection.execute(
                    """
                    INSERT INTO task_activity (task_id, entry_index, created_at, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (self.task.id, entry_index, entry.created_at, payload),
                )
            TaskEventLog(self.workspace).append(
                event_type="task_reported",
                task_id=self.task.id,
                payload={"activity": [entry.model_dump(mode="json") for entry in activity]},
            )
            connection.commit()

    def append(self, entry: TaskActivityEntry) -> None:
        """
        Append one verdict or report entry to this task's activity feed.

        Pays the load+rewrite cost on every append so persisted row
        order tracks arrival order without callers managing indexes.
        """
        activity = self.load()
        activity.append(entry)
        self.save(activity)

    def latest_entry(
        self,
        role: str | None = None,
        stage: TaskActivityStage | str | None = None,
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
        for entry in reversed(self.load()):
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

    def latest(self) -> TaskActivityEntry | None:
        """
        Return the newest activity entry regardless of filters.
        """
        return self.latest_entry()


def task_activity_store_for_task(workspace: Workspace, task: TaskRecord) -> TaskActivityStore:
    """
    Assemble the task-bound activity store.
    """
    return TaskActivityStore(workspace, task)


def _parse_created_at(value: str) -> datetime:
    """
    Parse the ``created_at`` ISO string into a UTC-aware datetime.

    Normalises trailing ``Z`` to ``+00:00`` and naive datetimes to UTC so
    ``TaskActivityStore.latest_entry``'s ``after`` filter can compare entries
    written by older code (no timezone) against newer UTC-aware writers
    without raising on the mixed-shape comparison.
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
