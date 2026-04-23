"""SQLite-backed runtime storage for workspace execution state."""

import json
import logging
import re
import sqlite3
from pathlib import Path

import yaml

from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow
from litehive.domain.runtime import TaskRuntime
from litehive.domain.task import TaskIntentRecord, TaskRecord, TaskStateRecord, WorkspaceState
from litehive.tasks.audit import TaskAuditEntry, insert_task_audit_entries

logger = logging.getLogger(__name__)

_TASK_DIR_RE = re.compile(r"^T-(\d{4})-")
_TASK_SCOPED_TABLES = (
    "task_state",
    "task_journal",
    "task_activity",
    "stage_reports",
    "hook_artifacts",
    "subagent_sessions",
    "events",
    "attention",
    "worktrees",
    "pipeline_transitions",
    "pipeline_journal",
    "pipeline_task_state",
    "pipeline_sessions",
)


class RuntimeStore:
    """Small repository-style API over the workspace runtime database."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def bootstrap(self) -> None:
        with connect_workspace_db(self.root) as connection:
            self._ensure_workspace_state_rows(connection)
            disk_task_ids, highest_task_number = self._task_ids_on_disk()
            if not self._runtime_state_is_current(
                connection,
                task_ids_on_disk=disk_task_ids,
                highest_task_number=highest_task_number,
            ):
                seeded_task_ids = self._seed_task_state_rows_from_disk(connection)
                self._seed_workspace_state_from_disk(connection, seeded_task_ids)
            connection.commit()

    def load_workspace_state(self) -> WorkspaceState | None:
        with connect_workspace_db(self.root) as connection:
            self._ensure_workspace_state_rows(connection)
            state_row = connection.execute(
                "SELECT payload FROM pool_state WHERE workspace_key = ?",
                ("workspace",),
            ).fetchone()
            queue_row = connection.execute(
                "SELECT payload FROM queue WHERE workspace_key = ?",
                ("workspace",),
            ).fetchone()
        if state_row is None or queue_row is None:
            return None
        payload = json.loads(state_row["payload"])
        payload["queue"] = json.loads(queue_row["payload"])
        return WorkspaceState(**payload)

    def save_workspace_state(self, state: WorkspaceState) -> None:
        with connect_workspace_db(self.root) as connection:
            self._save_workspace_state(connection, state)
            connection.commit()

    def load_task_state(self, task_id: str) -> TaskStateRecord | None:
        with connect_workspace_db(self.root) as connection:
            row = connection.execute(
                "SELECT payload FROM task_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        return TaskStateRecord(**payload)

    def save_task_state(self, task_id: str, state: TaskStateRecord) -> None:
        with connect_workspace_db(self.root) as connection:
            self._save_task_state(connection, task_id, state)
            connection.commit()

    def save_runtime_transaction(
        self,
        *,
        task_states: dict[str, TaskStateRecord] | None = None,
        workspace_state: WorkspaceState | None = None,
        audit_entries: list[TaskAuditEntry] | None = None,
    ) -> None:
        with connect_workspace_db(self.root) as connection:
            if workspace_state is not None:
                self._save_workspace_state(connection, workspace_state)
            for task_id, state in (task_states or {}).items():
                self._save_task_state(connection, task_id, state)
            insert_task_audit_entries(connection, audit_entries or [])
            connection.commit()

    def delete_task_records_preserving_audit(
        self,
        task_id: str,
        *,
        audit_entries: list[TaskAuditEntry] | None = None,
    ) -> None:
        with connect_workspace_db(self.root) as connection:
            for table_name in _TASK_SCOPED_TABLES:
                connection.execute(f"DELETE FROM {table_name} WHERE task_id = ?", (task_id,))
            insert_task_audit_entries(connection, audit_entries or [])
            connection.commit()

    def _save_workspace_state(self, connection: sqlite3.Connection, state: WorkspaceState) -> None:
        now = utcnow()
        payload = state.model_dump(mode="json")
        queue_payload = json.dumps(payload.pop("queue"), sort_keys=True)
        connection.execute(
            """
            INSERT INTO pool_state (workspace_key, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(workspace_key) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            ("workspace", json.dumps(payload, sort_keys=True), now),
        )
        connection.execute(
            """
            INSERT INTO queue (workspace_key, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(workspace_key) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            ("workspace", queue_payload, now),
        )

    def load_task_runtime(self, task_id: str) -> TaskRuntime | None:
        state = self.load_task_state(task_id)
        if state is None:
            return None
        return state.runtime

    def save_task_runtime(self, task_id: str, runtime: TaskRuntime) -> None:
        state = self.load_task_state(task_id) or TaskStateRecord()
        state.runtime = runtime.model_copy(deep=True)
        state.updated_at = runtime.updated_at or state.updated_at or utcnow()
        self.save_task_state(task_id, state)

    def _save_task_state(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        state: TaskStateRecord,
    ) -> None:
        now = state.updated_at or state.runtime.updated_at or utcnow()
        state = state.model_copy(deep=True)
        state.updated_at = now
        if state.runtime.updated_at is None:
            state.runtime.updated_at = now
        payload = state.model_dump(mode="json")
        payload["updated_at"] = now
        connection.execute(
            """
            INSERT INTO task_state (task_id, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (task_id, json.dumps(payload, sort_keys=True), now),
        )

    @staticmethod
    def _ensure_workspace_state_rows(connection: sqlite3.Connection) -> None:
        now = utcnow()
        connection.execute(
            """
            INSERT OR IGNORE INTO pool_state (workspace_key, payload, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                "workspace",
                json.dumps(
                    WorkspaceState().model_dump(mode="json", exclude={"queue"}),
                    sort_keys=True,
                ),
                now,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO queue (workspace_key, payload, updated_at)
            VALUES (?, ?, ?)
            """,
            ("workspace", json.dumps([], sort_keys=True), now),
        )
        connection.commit()

    def _task_ids_on_disk(self) -> tuple[list[str], int]:
        task_ids: list[str] = []
        highest_task_number = 0
        tasks_dir = self.root / ".litehive" / "tasks"
        if not tasks_dir.exists():
            return task_ids, highest_task_number
        for child in tasks_dir.iterdir():
            if not child.is_dir():
                continue
            match = _TASK_DIR_RE.match(child.name)
            if match is None:
                continue
            task_ids.append(f"T-{match.group(1)}")
            highest_task_number = max(highest_task_number, int(match.group(1)))
        task_ids.sort()
        return task_ids, highest_task_number

    def _runtime_state_is_current(
        self,
        connection: sqlite3.Connection,
        *,
        task_ids_on_disk: list[str],
        highest_task_number: int,
    ) -> bool:
        state_row = connection.execute(
            "SELECT payload FROM pool_state WHERE workspace_key = ?",
            ("workspace",),
        ).fetchone()
        queue_row = connection.execute(
            "SELECT payload FROM queue WHERE workspace_key = ?",
            ("workspace",),
        ).fetchone()
        if state_row is None or queue_row is None:
            return False
        if not task_ids_on_disk:
            return True
        task_state_count = int(connection.execute("SELECT COUNT(*) FROM task_state").fetchone()[0])
        if task_state_count < len(task_ids_on_disk):
            return False
        payload = json.loads(state_row["payload"])
        queue = json.loads(queue_row["payload"])
        default_state_payload = WorkspaceState().model_dump(mode="json", exclude={"queue"})
        fresh_workspace_state = payload == default_state_payload and queue == []
        if fresh_workspace_state:
            return False
        return int(payload.get("next_task_number") or 0) >= highest_task_number

    def _seed_task_state_rows_from_disk(self, connection: sqlite3.Connection) -> list[str]:
        existing_rows = connection.execute("SELECT task_id FROM task_state").fetchall()
        existing_task_ids = {str(row["task_id"]) for row in existing_rows}
        task_ids_on_disk: list[str] = []
        tasks_dir = self.root / ".litehive" / "tasks"
        if not tasks_dir.exists():
            return task_ids_on_disk
        for task_file in sorted(tasks_dir.glob("*/task.yaml")):
            try:
                loaded = yaml.safe_load(task_file.read_text(encoding="utf-8")) or {}
                if not isinstance(loaded, dict):
                    raise ValueError("task file must contain a mapping")
                intent = TaskIntentRecord.model_validate(loaded)
            except Exception as exc:
                logger.warning("Skipping invalid task file during sqlite bootstrap %s: %s", task_file, exc)
                continue
            task = TaskRecord.from_intent_and_state(intent)
            task_ids_on_disk.append(task.id)
            if task.id in existing_task_ids:
                continue
            self._save_task_state(connection, task.id, task.to_storage_state_record())
        return task_ids_on_disk

    def _seed_workspace_state_from_disk(
        self,
        connection: sqlite3.Connection,
        task_ids_on_disk: list[str],
    ) -> None:
        state_row = connection.execute(
            "SELECT payload FROM pool_state WHERE workspace_key = ?",
            ("workspace",),
        ).fetchone()
        queue_row = connection.execute(
            "SELECT payload FROM queue WHERE workspace_key = ?",
            ("workspace",),
        ).fetchone()
        if state_row is None or queue_row is None:
            return
        payload = json.loads(state_row["payload"])
        queue = json.loads(queue_row["payload"])
        highest_task_number = 0
        for task_id in task_ids_on_disk:
            try:
                highest_task_number = max(highest_task_number, int(task_id.split("-", 1)[1]))
            except (IndexError, ValueError):
                continue
        default_state_payload = WorkspaceState().model_dump(mode="json", exclude={"queue"})
        fresh_workspace_state = payload == default_state_payload and queue == []
        if not fresh_workspace_state and highest_task_number <= int(payload.get("next_task_number") or 0):
            return
        state = WorkspaceState(**payload, queue=queue)
        if fresh_workspace_state and task_ids_on_disk:
            state.queue = list(task_ids_on_disk)
        if highest_task_number > state.next_task_number:
            state.next_task_number = highest_task_number
        self._save_workspace_state(connection, state)


def runtime_store(root: Path) -> RuntimeStore:
    return RuntimeStore(root)
