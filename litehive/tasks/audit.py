"""Durable append-only task audit log backed by the workspace database."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow
from litehive.domain.task import TaskRecord
from litehive.tasks.event_log import append_task_event, task_event_type_for_audit_action


@dataclass(frozen=True)
class TaskAuditState:
    """Lightweight task snapshot for audit rows."""

    status: str
    pipeline_status: str


class TaskAuditEntry(BaseModel):
    """Structured audit record for a task lifecycle or queue mutation."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    task_id: str
    created_at: str = Field(default_factory=utcnow)
    action: str
    actor: str
    source: str
    task_status_before: str | None = None
    task_status_after: str | None = None
    pipeline_status_before: str | None = None
    pipeline_status_after: str | None = None
    queue_position_before: int | None = None
    queue_position_after: int | None = None
    context: dict[str, Any] = Field(default_factory=dict)


def snapshot_task_audit_state(task: TaskRecord | None) -> TaskAuditState | None:
    if task is None:
        return None
    return TaskAuditState(status=str(task.status), pipeline_status=str(task.pipeline_status))


def queue_position(queue: list[str] | tuple[str, ...], task_id: str) -> int | None:
    try:
        return list(queue).index(task_id) + 1
    except ValueError:
        return None


def build_task_audit_entry(
    *,
    task_id: str,
    action: str,
    actor: str,
    source: str,
    before_task: TaskRecord | TaskAuditState | None = None,
    after_task: TaskRecord | TaskAuditState | None = None,
    before_queue: list[str] | tuple[str, ...] | None = None,
    after_queue: list[str] | tuple[str, ...] | None = None,
    context: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> TaskAuditEntry:
    return TaskAuditEntry(
        task_id=task_id,
        created_at=created_at or utcnow(),
        action=action,
        actor=actor,
        source=source,
        task_status_before=None if before_task is None else str(before_task.status),
        task_status_after=None if after_task is None else str(after_task.status),
        pipeline_status_before=None if before_task is None else str(before_task.pipeline_status),
        pipeline_status_after=None if after_task is None else str(after_task.pipeline_status),
        queue_position_before=None if before_queue is None else queue_position(before_queue, task_id),
        queue_position_after=None if after_queue is None else queue_position(after_queue, task_id),
        context=dict(context or {}),
    )


def insert_task_audit_entries(connection: sqlite3.Connection, entries: Iterable[TaskAuditEntry]) -> None:
    rows = [
        (
            entry.task_id,
            entry.created_at,
            entry.action,
            entry.actor,
            entry.source,
            entry.task_status_before,
            entry.task_status_after,
            entry.pipeline_status_before,
            entry.pipeline_status_after,
            entry.queue_position_before,
            entry.queue_position_after,
            json.dumps(entry.context, sort_keys=True),
        )
        for entry in entries
    ]
    if not rows:
        return
    connection.executemany(
        """
        INSERT INTO task_audit_log (
            task_id,
            created_at,
            action,
            actor,
            source,
            task_status_before,
            task_status_after,
            pipeline_status_before,
            pipeline_status_after,
            queue_position_before,
            queue_position_after,
            context_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def append_task_audit_entries(root: Path, entries: Iterable[TaskAuditEntry]) -> None:
    entry_list = list(entries)
    if not entry_list:
        return
    with connect_workspace_db(root) as connection:
        insert_task_audit_entries(connection, entry_list)
        for entry in entry_list:
            append_task_event(
                root,
                event_type=task_event_type_for_audit_action(entry.action),
                task_id=entry.task_id,
                payload={"audit_entry": entry.model_dump(mode="json")},
            )
        connection.commit()


def load_task_audit_entries(
    root: Path,
    *,
    task_id: str | None = None,
    action: str | None = None,
    limit: int = 20,
) -> list[TaskAuditEntry]:
    query = """
        SELECT
            id,
            task_id,
            created_at,
            action,
            actor,
            source,
            task_status_before,
            task_status_after,
            pipeline_status_before,
            pipeline_status_after,
            queue_position_before,
            queue_position_after,
            context_json
        FROM task_audit_log
    """
    clauses: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(task_id)
    if action is not None:
        clauses.append("action = ?")
        params.append(action)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with connect_workspace_db(root) as connection:
        rows = connection.execute(query, params).fetchall()

    entries: list[TaskAuditEntry] = []
    for row in rows:
        try:
            context = json.loads(str(row["context_json"]))
        except json.JSONDecodeError:
            context = {}
        if not isinstance(context, dict):
            context = {}
        entries.append(
            TaskAuditEntry(
                id=int(row["id"]),
                task_id=str(row["task_id"]),
                created_at=str(row["created_at"]),
                action=str(row["action"]),
                actor=str(row["actor"]),
                source=str(row["source"]),
                task_status_before=(None if row["task_status_before"] is None else str(row["task_status_before"])),
                task_status_after=(None if row["task_status_after"] is None else str(row["task_status_after"])),
                pipeline_status_before=(
                    None if row["pipeline_status_before"] is None else str(row["pipeline_status_before"])
                ),
                pipeline_status_after=(
                    None if row["pipeline_status_after"] is None else str(row["pipeline_status_after"])
                ),
                queue_position_before=(
                    None if row["queue_position_before"] is None else int(row["queue_position_before"])
                ),
                queue_position_after=(
                    None if row["queue_position_after"] is None else int(row["queue_position_after"])
                ),
                context=context,
            )
        )
    return entries
