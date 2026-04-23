"""SQLite-backed runtime storage for workspace execution state."""

import json
import sqlite3
from pathlib import Path

from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow
from litehive.domain.runtime import TaskRuntime
from litehive.domain.task import TaskStateRecord, WorkspaceState
from litehive.tasks.audit import TaskAuditEntry, insert_task_audit_entries

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


def runtime_store(root: Path) -> RuntimeStore:
    return RuntimeStore(root)
