"""SQLite-backed runtime storage for workspace execution state."""

import json
import logging
import re
import sqlite3
from pathlib import Path

from litehive.db.schema import connect_workspace_db, consume_rebuilt_database_marker
from litehive.domain.common import utcnow
from litehive.domain.runtime import TaskRuntime
from litehive.domain.task import TaskIntentRecord, TaskStateRecord, WorkspaceState
from litehive.tasks.audit import TaskAuditEntry, insert_task_audit_entries
from litehive.tasks.event_log import append_task_event, task_event_type_for_audit_action

logger = logging.getLogger(__name__)

_TASK_DIR_RE = re.compile(r"^T-(\d{4})-")
_TASK_SCOPED_TABLES = (
    "task_state",
    "task_intent",
    "task_journal",
    "task_activity",
    "stage_reports",
    "recovery_reports",
    "hook_artifacts",
    "subagent_sessions",
    "events",
    "attention",
    "worktrees",
    "pipeline_transitions",
    "pipeline_journal",
    "pipeline_task_state",
    "pipeline_sessions",
    # task_audit_log is intentionally excluded: hard deletes must keep
    # durable lifecycle history and tombstones queryable after the task rows go away.
)


class RuntimeStore:
    """Small repository-style API over the workspace runtime database."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def bootstrap(self) -> None:
        """Initialize workspace rows and replay the task event log after DB loss."""
        with connect_workspace_db(self.root) as connection:
            self.ensure_workspace_state_rows(connection)
            if consume_rebuilt_database_marker(self.root):
                connection.commit()
            connection.commit()
        if self._should_rebuild_from_task_event_log():
            from litehive.tasks.event_log import rebuild_sqlite_from_task_event_log

            rebuild_sqlite_from_task_event_log(self.root)

    def _should_rebuild_from_task_event_log(self) -> bool:
        from litehive.tasks.event_log import sqlite_task_tables_empty, task_event_log_has_events

        return task_event_log_has_events(self.root) and sqlite_task_tables_empty(self.root)

    def load_workspace_state(self) -> WorkspaceState | None:
        with connect_workspace_db(self.root) as connection:
            self.ensure_workspace_state_rows(connection)
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
            self._append_workspace_state_event(state)
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

    def load_task_intent(self, task_id: str) -> TaskIntentRecord | None:
        with connect_workspace_db(self.root) as connection:
            row = connection.execute(
                "SELECT payload FROM task_intent WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        return TaskIntentRecord.model_validate(payload)

    def list_task_intents(self) -> list[TaskIntentRecord]:
        with connect_workspace_db(self.root) as connection:
            rows = connection.execute(
                "SELECT payload FROM task_intent ORDER BY task_id ASC",
            ).fetchall()
        intents: list[TaskIntentRecord] = []
        for row in rows:
            payload = json.loads(row["payload"])
            intents.append(TaskIntentRecord.model_validate(payload))
        return intents

    def save_task_intent(self, task_id: str, intent: TaskIntentRecord) -> None:
        with connect_workspace_db(self.root) as connection:
            self._save_task_intent(connection, task_id, intent)
            append_task_event(
                self.root,
                event_type="task_intent_saved",
                task_id=task_id,
                payload={"task_intent": intent.model_dump(mode="json")},
            )
            connection.commit()

    def save_task_state(self, task_id: str, state: TaskStateRecord) -> None:
        with connect_workspace_db(self.root) as connection:
            self._save_task_state(connection, task_id, state)
            append_task_event(
                self.root,
                event_type="task_state_saved",
                task_id=task_id,
                payload={"task_state": state.model_dump(mode="json")},
            )
            connection.commit()

    def save_runtime_transaction(
        self,
        *,
        task_intents: dict[str, TaskIntentRecord] | None = None,
        task_states: dict[str, TaskStateRecord] | None = None,
        workspace_state: WorkspaceState | None = None,
        audit_entries: list[TaskAuditEntry] | None = None,
    ) -> None:
        with connect_workspace_db(self.root) as connection:
            if workspace_state is not None:
                self._save_workspace_state(connection, workspace_state)
            for task_id, intent in (task_intents or {}).items():
                self._save_task_intent(connection, task_id, intent)
            for task_id, state in (task_states or {}).items():
                self._save_task_state(connection, task_id, state)
            insert_task_audit_entries(connection, audit_entries or [])
            self._append_runtime_transaction_events(
                task_intents=task_intents or {},
                task_states=task_states or {},
                workspace_state=workspace_state,
                audit_entries=audit_entries or [],
            )
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
            if audit_entries:
                for entry in audit_entries:
                    append_task_event(
                        self.root,
                        event_type=task_event_type_for_audit_action(entry.action),
                        task_id=entry.task_id,
                        payload={"audit_entry": entry.model_dump(mode="json")},
                    )
            else:
                append_task_event(
                    self.root,
                    event_type="task_deleted",
                    task_id=task_id,
                    payload={},
                )
            connection.commit()

    def _append_workspace_state_event(self, state: WorkspaceState) -> None:
        append_task_event(
            self.root,
            event_type="workspace_state_saved",
            task_id=None,
            payload={"workspace_state": state.model_dump(mode="json")},
        )

    def _append_runtime_transaction_events(
        self,
        *,
        task_intents: dict[str, TaskIntentRecord],
        task_states: dict[str, TaskStateRecord],
        workspace_state: WorkspaceState | None,
        audit_entries: list[TaskAuditEntry],
    ) -> None:
        logged_task_ids: set[str] = set()
        workspace_payload = None if workspace_state is None else workspace_state.model_dump(mode="json")

        def payload_for_task(task_id: str) -> dict[str, object]:
            payload: dict[str, object] = {}
            if task_id in task_intents:
                payload["task_intent"] = task_intents[task_id].model_dump(mode="json")
            if task_id in task_states:
                payload["task_state"] = task_states[task_id].model_dump(mode="json")
            if workspace_payload is not None:
                payload["workspace_state"] = workspace_payload
            return payload

        for entry in audit_entries:
            payload = payload_for_task(entry.task_id)
            payload["audit_entry"] = entry.model_dump(mode="json")
            append_task_event(
                self.root,
                event_type=task_event_type_for_audit_action(entry.action),
                task_id=entry.task_id,
                payload=payload,
            )
            logged_task_ids.add(entry.task_id)

        for task_id in sorted((set(task_intents) | set(task_states)) - logged_task_ids):
            append_task_event(
                self.root,
                event_type="task_state_saved",
                task_id=task_id,
                payload=payload_for_task(task_id),
            )
            logged_task_ids.add(task_id)

        if workspace_state is not None and not logged_task_ids:
            self._append_workspace_state_event(workspace_state)

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
        state.updated_at = runtime.pipeline.updated_at or state.updated_at or utcnow()
        self.save_task_state(task_id, state)

    def _save_task_state(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        state: TaskStateRecord,
    ) -> None:
        now = state.updated_at or state.runtime.pipeline.updated_at or utcnow()
        state = state.model_copy(deep=True)
        state.updated_at = now
        if state.runtime.pipeline.updated_at is None:
            state.runtime.pipeline.updated_at = now
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

    def _save_task_intent(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        intent: TaskIntentRecord,
    ) -> None:
        now = utcnow()
        payload = intent.model_dump(mode="json")
        connection.execute(
            """
            INSERT INTO task_intent (task_id, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (task_id, json.dumps(payload, sort_keys=True), now),
        )

    def highest_task_number(self) -> int:
        task_ids: set[str] = set()
        with connect_workspace_db(self.root) as connection:
            for table_name in ("task_intent", "task_state"):
                rows = connection.execute(f"SELECT task_id FROM {table_name}").fetchall()
                task_ids.update(str(row["task_id"]) for row in rows)
        highest = 0
        for task_id in task_ids:
            match = _TASK_DIR_RE.match(f"{task_id}-")
            if match is None:
                continue
            highest = max(highest, int(match.group(1)))
        return highest

    @staticmethod
    def ensure_workspace_state_rows(connection: sqlite3.Connection) -> None:
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
