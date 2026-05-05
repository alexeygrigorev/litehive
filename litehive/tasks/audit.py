"""Durable append-only task audit log backed by the workspace database."""

from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from litehive.domain.common import utcnow
from litehive.domain.task import TaskRecord
from litehive.tasks.event_log import append_task_event, task_event_type_for_audit_action
from litehive.workspace import Workspace


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
    """Freeze a task's status fields into the lightweight before/after shape the audit log stores; capturing the snapshot at the call site lets the caller mutate the live record afterwards without losing the original values."""
    if task is None:
        return None
    return TaskAuditState(status=str(task.status), pipeline_status=str(task.pipeline_status))


def queue_position(queue: list[str] | tuple[str, ...], task_id: str) -> int | None:
    """Locate a task within the runtime queue ordering for audit context; returns a 1-indexed position so the audit log reads naturally to operators ("position 3 of 7") rather than as raw list indices."""
    try:
        return list(queue).index(task_id) + 1
    except ValueError:
        return None


def build_task_audit_entry(
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
    """Compose a structured audit row from the call-site's before/after snapshots; used by every task-mutation surface (CLI, recovery, lifecycle) so the schema stays uniform and queryable."""
    if before_task is None:
        task_status_before = None
        pipeline_status_before = None
    else:
        task_status_before = str(before_task.status)
        pipeline_status_before = str(before_task.pipeline_status)
    if after_task is None:
        task_status_after = None
        pipeline_status_after = None
    else:
        task_status_after = str(after_task.status)
        pipeline_status_after = str(after_task.pipeline_status)
    if before_queue is None:
        queue_position_before = None
    else:
        queue_position_before = queue_position(before_queue, task_id)
    if after_queue is None:
        queue_position_after = None
    else:
        queue_position_after = queue_position(after_queue, task_id)
    return TaskAuditEntry(
        task_id=task_id,
        created_at=created_at or utcnow(),
        action=action,
        actor=actor,
        source=source,
        task_status_before=task_status_before,
        task_status_after=task_status_after,
        pipeline_status_before=pipeline_status_before,
        pipeline_status_after=pipeline_status_after,
        queue_position_before=queue_position_before,
        queue_position_after=queue_position_after,
        context=dict(context or {}),
    )


def insert_task_audit_entries(connection: sqlite3.Connection, entries: Iterable[TaskAuditEntry]) -> None:
    """Bulk-insert audit rows on an open connection; used by the persistence layer so an audit batch lands in the same transaction as the task-state mutation it describes."""
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


def append_task_audit_entries(workspace: Workspace, entries: Iterable[TaskAuditEntry]) -> None:
    """Top-level "log these audit entries" call: writes audit rows and the parallel task-event entries in one transaction so external observers see the audit and the event log progress together."""
    entry_list = list(entries)
    if not entry_list:
        return
    with workspace.connect() as connection:
        insert_task_audit_entries(connection, entry_list)
        for entry in entry_list:
            append_task_event(
                workspace,
                event_type=task_event_type_for_audit_action(entry.action),
                task_id=entry.task_id,
                payload={"audit_entry": entry.model_dump(mode="json")},
            )
        connection.commit()


def load_task_audit_entries(
    workspace: Workspace,
    task_id: str | None = None,
    action: str | None = None,
    limit: int = 20,
) -> list[TaskAuditEntry]:
    """Read recent audit rows for the operator-facing audit/diagnostics CLI; returns most-recent-first because the audit UI is a tail view, not a forward replay."""
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

    with workspace.connect() as connection:
        rows = connection.execute(query, params).fetchall()

    entries: list[TaskAuditEntry] = []
    for row in rows:
        try:
            context = json.loads(str(row["context_json"]))
        except json.JSONDecodeError:
            context = {}
        if not isinstance(context, dict):
            context = {}
        if row["task_status_before"] is None:
            task_status_before = None
        else:
            task_status_before = str(row["task_status_before"])
        if row["task_status_after"] is None:
            task_status_after = None
        else:
            task_status_after = str(row["task_status_after"])
        if row["pipeline_status_before"] is None:
            pipeline_status_before = None
        else:
            pipeline_status_before = str(row["pipeline_status_before"])
        if row["pipeline_status_after"] is None:
            pipeline_status_after = None
        else:
            pipeline_status_after = str(row["pipeline_status_after"])
        if row["queue_position_before"] is None:
            queue_position_before = None
        else:
            queue_position_before = int(row["queue_position_before"])
        if row["queue_position_after"] is None:
            queue_position_after = None
        else:
            queue_position_after = int(row["queue_position_after"])
        entries.append(
            TaskAuditEntry(
                id=int(row["id"]),
                task_id=str(row["task_id"]),
                created_at=str(row["created_at"]),
                action=str(row["action"]),
                actor=str(row["actor"]),
                source=str(row["source"]),
                task_status_before=task_status_before,
                task_status_after=task_status_after,
                pipeline_status_before=pipeline_status_before,
                pipeline_status_after=pipeline_status_after,
                queue_position_before=queue_position_before,
                queue_position_after=queue_position_after,
                context=context,
            )
        )
    return entries
