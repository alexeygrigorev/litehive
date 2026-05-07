"""
Migration 0007 task_intent denormalized-column backfill.

Migration 0007 added query-friendly columns alongside the persisted
task-intent JSON payload. This hook is migration-only data code: it
projects existing task intent/state payloads into the new columns once
immediately after the SQL migration applies.
"""

import json
import logging
import sqlite3

from pydantic import ValidationError

from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.task import TaskIntentRecord, TaskStateRecord

logger = logging.getLogger(__name__)


def task_intent_column_values(
    intent: TaskIntentRecord,
    state: TaskStateRecord | None = None,
) -> dict[str, str]:
    """
    Project a ``TaskIntentRecord`` / ``TaskStateRecord`` pair onto migration 0007 columns.
    """
    if intent.created_from is None:
        provenance_payload: dict[str, object] = {}
    else:
        provenance_payload = intent.created_from.model_dump(mode="json")
    if state is None:
        lifecycle_status = TaskStatus.QUEUED.value
        pipeline_status = PipelineStatus.BACKLOG.value
    else:
        lifecycle_status = state.status.value
        pipeline_status = state.pipeline_status.value
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
        "provenance_json": json.dumps(provenance_payload, sort_keys=True),
        "lifecycle_status": lifecycle_status,
        "pipeline_status": pipeline_status,
    }


def sync_task_intent_columns(connection: sqlite3.Connection) -> None:
    """
    Backfill denormalized ``task_intent`` columns from each row's JSON payload.
    """
    rows = connection.execute(
        """
        SELECT intent.task_id, intent.payload AS intent_payload, state.payload AS state_payload
        FROM task_intent AS intent
        LEFT JOIN task_state AS state ON state.task_id = intent.task_id
        """
    ).fetchall()
    for row in rows:
        try:
            intent_payload = json.loads(str(row["intent_payload"]))
            intent = TaskIntentRecord.model_validate(intent_payload)
            state = None
            if row["state_payload"] is not None:
                state_payload = json.loads(str(row["state_payload"]))
                state = TaskStateRecord.model_validate(state_payload)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            logger.warning("Skipping invalid task_intent column backfill for %s: %s", row["task_id"], exc)
            continue
        values = task_intent_column_values(intent, state)
        connection.execute(
            """
            UPDATE task_intent
            SET
                slug = ?,
                title = ?,
                created_at = ?,
                priority = ?,
                goal = ?,
                acceptance_criteria_json = ?,
                constraints_json = ?,
                plan_json = ?,
                dependencies_json = ?,
                provenance_json = ?,
                lifecycle_status = ?,
                pipeline_status = ?
            WHERE task_id = ?
            """,
            (
                values["slug"],
                values["title"],
                values["created_at"],
                values["priority"],
                values["goal"],
                values["acceptance_criteria_json"],
                values["constraints_json"],
                values["plan_json"],
                values["dependencies_json"],
                values["provenance_json"],
                values["lifecycle_status"],
                values["pipeline_status"],
                row["task_id"],
            ),
        )
