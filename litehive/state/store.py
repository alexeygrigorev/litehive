"""SQLite-backed runtime storage for workspace execution state."""

import json
import logging
import re
import sqlite3
from pathlib import Path

from litehive.config.paths import workspace_path
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

    def load_workspace_state_read_only(self) -> WorkspaceState | None:
        db_path = workspace_path(self.root, "data.db")
        if not db_path.exists():
            return None
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
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
        task_journal_messages: dict[str, str] | None = None,
        audit_entries: list[TaskAuditEntry] | None = None,
    ) -> None:
        with connect_workspace_db(self.root) as connection:
            if workspace_state is not None:
                self._save_workspace_state(connection, workspace_state)
            for task_id, intent in (task_intents or {}).items():
                self._save_task_intent(connection, task_id, intent)
            for task_id, state in (task_states or {}).items():
                self._save_task_state(connection, task_id, state)
            task_journal_entries: dict[str, list[dict[str, object]]] = {}
            for task_id, message in (task_journal_messages or {}).items():
                entry = self._append_task_journal(connection, task_id, message)
                task_journal_entries.setdefault(task_id, []).append(entry)
            insert_task_audit_entries(connection, audit_entries or [])
            self._append_runtime_transaction_events(
                task_intents=task_intents or {},
                task_states=task_states or {},
                workspace_state=workspace_state,
                task_journal_entries=task_journal_entries,
                audit_entries=audit_entries or [],
            )
            connection.commit()

    def delete_task_records(
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
        task_journal_entries: dict[str, list[dict[str, object]]],
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
            if task_id in task_journal_entries:
                payload["task_journal"] = task_journal_entries[task_id]
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

        for task_id in sorted(set(task_journal_entries) - logged_task_ids):
            append_task_event(
                self.root,
                event_type="task_journal_recorded",
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
        connection.execute(
            """
            UPDATE task_intent
            SET lifecycle_status = ?, pipeline_status = ?
            WHERE task_id = ?
            """,
            (str(state.status), str(state.pipeline_status), task_id),
        )

    def _save_task_intent(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        intent: TaskIntentRecord,
    ) -> None:
        now = utcnow()
        payload = intent.model_dump(mode="json")
        existing_state = _load_task_state_for_intent_columns(connection, task_id)
        column_values = _task_intent_column_values(intent, existing_state)
        connection.execute(
            """
            INSERT INTO task_intent (
                task_id,
                payload,
                updated_at,
                slug,
                title,
                created_at,
                priority,
                goal,
                acceptance_criteria_json,
                constraints_json,
                plan_json,
                dependencies_json,
                provenance_json,
                lifecycle_status,
                pipeline_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at,
                slug = excluded.slug,
                title = excluded.title,
                created_at = excluded.created_at,
                priority = excluded.priority,
                goal = excluded.goal,
                acceptance_criteria_json = excluded.acceptance_criteria_json,
                constraints_json = excluded.constraints_json,
                plan_json = excluded.plan_json,
                dependencies_json = excluded.dependencies_json,
                provenance_json = excluded.provenance_json
            """,
            (
                task_id,
                json.dumps(payload, sort_keys=True),
                now,
                column_values["slug"],
                column_values["title"],
                column_values["created_at"],
                column_values["priority"],
                column_values["goal"],
                column_values["acceptance_criteria_json"],
                column_values["constraints_json"],
                column_values["plan_json"],
                column_values["dependencies_json"],
                column_values["provenance_json"],
                column_values["lifecycle_status"],
                column_values["pipeline_status"],
            ),
        )

    def append_task_journal(self, task_id: str, message: str) -> None:
        with connect_workspace_db(self.root) as connection:
            entry = self._append_task_journal(connection, task_id, message)
            append_task_event(
                self.root,
                event_type="task_journal_recorded",
                task_id=task_id,
                payload={"task_journal": [entry]},
            )
            connection.commit()

    def _append_task_journal(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        message: str,
    ) -> dict[str, object]:
        created_at = utcnow()
        row = connection.execute(
            "SELECT COALESCE(MAX(entry_index) + 1, 0) AS next_index FROM task_journal WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        entry_index = 0 if row is None else int(row["next_index"])
        metadata = "{}"
        connection.execute(
            """
            INSERT INTO task_journal (task_id, entry_index, created_at, message, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, entry_index, created_at, message, metadata),
        )
        return {
            "task_id": task_id,
            "entry_index": entry_index,
            "created_at": created_at,
            "message": message,
            "metadata": {},
        }

    def save_process_state(
        self,
        process_key: str,
        *,
        status: str,
        payload: dict[str, object],
    ) -> None:
        now = utcnow()
        with connect_workspace_db(self.root) as connection:
            connection.execute(
                """
                INSERT INTO runtime_process_state (
                    process_key,
                    status,
                    pid,
                    workspace,
                    command,
                    active_task_id,
                    log_dir,
                    started_at,
                    heartbeat_at,
                    payload,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(process_key) DO UPDATE SET
                    status = excluded.status,
                    pid = excluded.pid,
                    workspace = excluded.workspace,
                    command = excluded.command,
                    active_task_id = excluded.active_task_id,
                    log_dir = excluded.log_dir,
                    started_at = excluded.started_at,
                    heartbeat_at = excluded.heartbeat_at,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    process_key,
                    status,
                    _optional_int(payload.get("pid")),
                    _optional_str(payload.get("workspace")),
                    _optional_str(payload.get("command")),
                    _optional_str(payload.get("active_task_id")),
                    _optional_str(payload.get("log_dir")),
                    _optional_str(payload.get("started_at")),
                    _optional_str(payload.get("heartbeat_at")),
                    json.dumps(payload, sort_keys=True),
                    now,
                ),
            )
            connection.commit()

    def clear_process_state(self, process_key: str) -> None:
        with connect_workspace_db(self.root) as connection:
            connection.execute("DELETE FROM runtime_process_state WHERE process_key = ?", (process_key,))
            connection.commit()

    def load_process_state(self, process_key: str) -> dict[str, object] | None:
        with connect_workspace_db(self.root) as connection:
            row = connection.execute(
                """
                SELECT
                    process_key,
                    status,
                    pid,
                    workspace,
                    command,
                    active_task_id,
                    log_dir,
                    started_at,
                    heartbeat_at,
                    payload
                FROM runtime_process_state
                WHERE process_key = ?
                """,
                (process_key,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, dict):
            return None
        for key in (
            "process_key",
            "status",
            "pid",
            "workspace",
            "command",
            "active_task_id",
            "log_dir",
            "started_at",
            "heartbeat_at",
        ):
            if payload.get(key) is None and row[key] is not None:
                payload[key] = row[key]
        return payload

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


def _load_task_state_for_intent_columns(
    connection: sqlite3.Connection,
    task_id: str,
) -> TaskStateRecord | None:
    row = connection.execute("SELECT payload FROM task_state WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload"]))
        return TaskStateRecord.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Ignoring invalid task_state row while saving task_intent columns for %s", task_id)
        return None


def _task_intent_column_values(
    intent: TaskIntentRecord,
    state: TaskStateRecord | None = None,
) -> dict[str, str]:
    return {
        "slug": intent.slug,
        "title": intent.title,
        "created_at": intent.created_at,
        "priority": intent.priority,
        "goal": intent.goal,
        "acceptance_criteria_json": json.dumps(intent.acceptance_criteria, sort_keys=True),
        "constraints_json": json.dumps(intent.constraints, sort_keys=True),
        "plan_json": json.dumps(intent.plan, sort_keys=True),
        "dependencies_json": json.dumps(intent.depends_on, sort_keys=True),
        "provenance_json": json.dumps(
            {} if intent.created_from is None else intent.created_from.model_dump(mode="json"),
            sort_keys=True,
        ),
        "lifecycle_status": "queued" if state is None else str(state.status),
        "pipeline_status": "backlog" if state is None else str(state.pipeline_status),
    }


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
