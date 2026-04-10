"""SQLite-backed runtime storage for workspace execution state."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from litehive.config.paths import workspace_database_path
from litehive.models import TaskRuntime, WorkspaceState, utcnow

logger = logging.getLogger(__name__)

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS pool_state (
        workspace_key TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS queue (
        workspace_key TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_state (
        task_id TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_journal (
        task_id TEXT NOT NULL,
        entry_index INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        message TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (task_id, entry_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stage_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        step TEXT NOT NULL,
        created_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hook_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT,
        hook_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subagent_sessions (
        task_id TEXT NOT NULL,
        subagent_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (task_id, subagent_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT,
        created_at TEXT NOT NULL,
        event_kind TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS engine_monitoring (
        engine_name TEXT PRIMARY KEY,
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attention (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT,
        created_at TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worktrees (
        task_id TEXT PRIMARY KEY,
        worktree_path TEXT,
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
)


def connect_workspace_db(root: Path) -> sqlite3.Connection:
    db_path = workspace_database_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.commit()
    return connection


class RuntimeStore:
    """Small repository-style API over the workspace runtime database."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def bootstrap(self) -> None:
        with connect_workspace_db(self.root) as connection:
            self._ensure_workspace_state_rows(connection)

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
        now = utcnow()
        payload = state.model_dump(mode="python")
        queue_payload = json.dumps(payload.pop("queue"), sort_keys=True)
        with connect_workspace_db(self.root) as connection:
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
            connection.commit()

    def load_task_runtime(self, task_id: str) -> TaskRuntime | None:
        with connect_workspace_db(self.root) as connection:
            row = connection.execute(
                "SELECT payload FROM task_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return TaskRuntime(**json.loads(row["payload"]))

    def save_task_runtime(self, task_id: str, runtime: TaskRuntime) -> None:
        now = runtime.updated_at or utcnow()
        payload = runtime.model_dump(mode="python")
        payload["updated_at"] = now
        with connect_workspace_db(self.root) as connection:
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
            connection.commit()

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
                    WorkspaceState().model_dump(mode="python", exclude={"queue"}),
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
