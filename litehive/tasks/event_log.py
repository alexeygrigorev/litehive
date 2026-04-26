"""Workspace-level append-only task event log and SQLite replay."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from litehive.config.paths import workspace_path
from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow
from litehive.domain.task import TaskIntentRecord, TaskStateRecord, WorkspaceState
from litehive.state.rebuild_safety import assert_database_rebuild_safe, backup_database_before_rebuild

TASK_EVENT_LOG_NAME = "task-events.jsonl"
TASK_EVENT_LOG_SCHEMA_VERSION = 1

_SUPPRESS_TASK_EVENT_LOGGING: ContextVar[bool] = ContextVar(
    "litehive_suppress_task_event_logging",
    default=False,
)

_ACTION_EVENT_TYPES = {
    "created": "task_created",
    "metadata_updated": "task_updated",
    "closed": "task_closed",
    "requeued": "task_requeued",
    "resumed": "task_resumed",
    "abandoned": "task_abandoned",
    "parked": "task_parked",
    "stopped": "task_stopped",
    "removed": "task_removed",
    "archived": "task_archived",
    "deleted": "task_deleted",
    "engine_switched": "task_engine_switched",
    "queue_enqueued": "task_queued",
    "queue_moved": "queue_reordered",
    "queue_prioritized": "queue_reordered",
}

_REPLAY_CLEARED_TABLES = (
    "task_state",
    "task_intent",
    "task_journal",
    "task_activity",
    "stage_reports",
    "recovery_reports",
    "pipeline_transitions",
    "pipeline_journal",
    "pipeline_task_state",
    "task_audit_log",
    "pool_state",
    "queue",
)


@dataclass(frozen=True, slots=True)
class TaskEventLogReplaySummary:
    """Summary of a task event log replay into SQLite."""

    event_log_path: Path
    events_seen: int
    events_replayed: int
    invalid_events: int
    tasks_rebuilt: int
    queue_length: int
    audit_entries: int
    activity_entries: int
    stage_reports: int
    recovery_reports: int


@dataclass(slots=True)
class _ReplayState:
    task_intents: dict[str, dict[str, Any]]
    task_states: dict[str, dict[str, Any]]
    task_journal: dict[str, list[dict[str, Any]]]
    task_activity: dict[str, list[dict[str, Any]]]
    task_audit_entries: list[dict[str, Any]]
    stage_reports: list[dict[str, Any]]
    recovery_reports: list[dict[str, Any]]
    pipeline_task_state: dict[str, dict[str, Any]]
    pipeline_transitions: list[dict[str, Any]]
    pipeline_journal: list[dict[str, Any]]
    workspace_state: dict[str, Any] | None = None

    @classmethod
    def empty(cls) -> "_ReplayState":
        return cls(
            workspace_state=None,
            task_intents={},
            task_states={},
            task_journal={},
            task_activity={},
            task_audit_entries=[],
            stage_reports=[],
            recovery_reports=[],
            pipeline_task_state={},
            pipeline_transitions=[],
            pipeline_journal=[],
        )


def task_event_log_path(root: Path) -> Path:
    """Return the workspace-level task event log path, outside SQLite."""

    return workspace_path(root, TASK_EVENT_LOG_NAME)


def task_event_logging_suppressed() -> bool:
    return _SUPPRESS_TASK_EVENT_LOGGING.get()


@contextmanager
def suppress_task_event_logging():
    token = _SUPPRESS_TASK_EVENT_LOGGING.set(True)
    try:
        yield
    finally:
        _SUPPRESS_TASK_EVENT_LOGGING.reset(token)


def task_event_type_for_audit_action(action: str) -> str:
    return _ACTION_EVENT_TYPES.get(action, f"task_{action}")


def append_task_event(
    root: Path,
    *,
    event_type: str,
    task_id: str | None,
    payload: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> dict[str, Any] | None:
    """Append one immutable task event record.

    Returns the event that was written. During replay the function is
    suppressed so rebuilding SQLite never creates synthetic history.
    """

    if task_event_logging_suppressed():
        return None
    event: dict[str, Any] = {
        "schema_version": TASK_EVENT_LOG_SCHEMA_VERSION,
        "timestamp": timestamp or utcnow(),
        "task_id": task_id,
        "event_type": event_type,
        "payload": dict(payload or {}),
    }
    path = task_event_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        if not os.environ.get("LITEHIVE_SKIP_FSYNC"):
            os.fsync(fd)
    finally:
        os.close(fd)
    return event


def read_task_events(root: Path) -> tuple[list[dict[str, Any]], int]:
    """Read complete JSON task events, skipping corrupt or truncated lines."""

    path = task_event_log_path(root)
    if not path.exists():
        return [], 0
    events: list[dict[str, Any]] = []
    invalid = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if not isinstance(event, dict):
            invalid += 1
            continue
        if not isinstance(event.get("payload"), dict):
            invalid += 1
            continue
        events.append(event)
    return events, invalid


def task_event_log_has_events(root: Path) -> bool:
    events, _invalid = read_task_events(root)
    return bool(events)


def rebuild_sqlite_from_task_event_log(root: Path, *, clear_existing: bool = True) -> TaskEventLogReplaySummary:
    """Replay the append-only task event log into the workspace SQLite DB."""

    events, invalid = read_task_events(root)
    replay_state = _ReplayState.empty()
    for event in events:
        _apply_event(replay_state, event)
    if clear_existing:
        replay_task_ids = set(replay_state.task_intents) | set(replay_state.task_states)
        db_path = workspace_path(root, "data.db")
        assert_database_rebuild_safe(root, db_path, replay_task_ids=replay_task_ids, operation="event-log replay")
        backup_database_before_rebuild(root, db_path, label="before-event-log-replay")
    with suppress_task_event_logging(), connect_workspace_db(root) as connection:
        if clear_existing:
            _clear_replay_tables(connection)
        _write_replay_state(connection, replay_state)
        connection.commit()
    activity_entries = sum(len(entries) for entries in replay_state.task_activity.values())
    workspace_state = _workspace_state_payload(replay_state)
    return TaskEventLogReplaySummary(
        event_log_path=task_event_log_path(root),
        events_seen=len(events) + invalid,
        events_replayed=len(events),
        invalid_events=invalid,
        tasks_rebuilt=len(replay_state.task_intents),
        queue_length=len(workspace_state.get("queue") or []),
        audit_entries=len(replay_state.task_audit_entries),
        activity_entries=activity_entries,
        stage_reports=len(replay_state.stage_reports),
        recovery_reports=len(replay_state.recovery_reports),
    )


def sqlite_task_tables_empty(root: Path) -> bool:
    with connect_workspace_db(root) as connection:
        task_intent_count = connection.execute("SELECT COUNT(*) FROM task_intent").fetchone()[0]
        task_state_count = connection.execute("SELECT COUNT(*) FROM task_state").fetchone()[0]
    return int(task_intent_count) == 0 and int(task_state_count) == 0


def _apply_event(replay_state: _ReplayState, event: dict[str, Any]) -> None:
    event_type = str(event.get("event_type") or "")
    task_id = _optional_str(event.get("task_id"))
    payload = dict(event.get("payload") or {})

    if event_type in {"task_deleted", "task_removed"} and task_id:
        _delete_task_scoped_replay_state(replay_state, task_id)

    workspace_state = payload.get("workspace_state")
    if isinstance(workspace_state, dict):
        replay_state.workspace_state = dict(workspace_state)

    task_intent = payload.get("task_intent")
    if task_id and isinstance(task_intent, dict):
        replay_state.task_intents[task_id] = dict(task_intent)

    task_state = payload.get("task_state")
    if task_id and isinstance(task_state, dict):
        replay_state.task_states[task_id] = dict(task_state)

    activity = payload.get("activity")
    if task_id and isinstance(activity, list):
        replay_state.task_activity[task_id] = [dict(entry) for entry in activity if isinstance(entry, dict)]

    task_journal = payload.get("task_journal")
    if task_id and isinstance(task_journal, list):
        replay_state.task_journal.setdefault(task_id, []).extend(
            dict(entry) for entry in task_journal if isinstance(entry, dict)
        )

    audit_entry = payload.get("audit_entry")
    if isinstance(audit_entry, dict):
        replay_state.task_audit_entries.append(dict(audit_entry))

    stage_report = payload.get("stage_report")
    if isinstance(stage_report, dict):
        replay_state.stage_reports.append(dict(stage_report))

    rewritten_stage_report = payload.get("rewritten_stage_report")
    if isinstance(rewritten_stage_report, dict):
        _rewrite_latest_stage_report(replay_state, rewritten_stage_report)

    recovery_report = payload.get("recovery_report")
    if isinstance(recovery_report, dict):
        replay_state.recovery_reports.append(dict(recovery_report))

    pipeline_state = payload.get("pipeline_task_state")
    if task_id and isinstance(pipeline_state, dict):
        replay_state.pipeline_task_state[task_id] = dict(pipeline_state)

    if event_type == "pipeline_task_state_reset" and task_id:
        replay_state.pipeline_task_state.pop(task_id, None)

    transition = payload.get("pipeline_transition")
    if isinstance(transition, dict):
        replay_state.pipeline_transitions.append(dict(transition))

    lifecycle = payload.get("pipeline_journal")
    if isinstance(lifecycle, dict):
        replay_state.pipeline_journal.append(dict(lifecycle))


def _delete_task_scoped_replay_state(replay_state: _ReplayState, task_id: str) -> None:
    replay_state.task_intents.pop(task_id, None)
    replay_state.task_states.pop(task_id, None)
    replay_state.task_journal.pop(task_id, None)
    replay_state.task_activity.pop(task_id, None)
    replay_state.pipeline_task_state.pop(task_id, None)
    replay_state.stage_reports = [
        report for report in replay_state.stage_reports if str(report.get("task_id")) != task_id
    ]
    replay_state.recovery_reports = [
        report for report in replay_state.recovery_reports if str(report.get("task_id")) != task_id
    ]
    replay_state.pipeline_transitions = [
        row for row in replay_state.pipeline_transitions if str(row.get("task_id")) != task_id
    ]
    replay_state.pipeline_journal = [row for row in replay_state.pipeline_journal if str(row.get("task_id")) != task_id]


def _rewrite_latest_stage_report(replay_state: _ReplayState, report: dict[str, Any]) -> None:
    task_id = str(report.get("task_id") or "")
    pipeline_state = str(report.get("pipeline_state") or report.get("stage") or "")
    for index in range(len(replay_state.stage_reports) - 1, -1, -1):
        existing = replay_state.stage_reports[index]
        existing_state = str(existing.get("pipeline_state") or existing.get("stage") or "")
        if str(existing.get("task_id") or "") == task_id and existing_state == pipeline_state:
            replay_state.stage_reports[index] = dict(report)
            return
    replay_state.stage_reports.append(dict(report))


def _clear_replay_tables(connection: sqlite3.Connection) -> None:
    for table_name in _REPLAY_CLEARED_TABLES:
        connection.execute(f"DELETE FROM {table_name}")


def _write_replay_state(connection: sqlite3.Connection, replay_state: _ReplayState) -> None:
    workspace_state = _workspace_state_payload(replay_state)
    _insert_workspace_state(connection, workspace_state)
    for task_id, intent in sorted(replay_state.task_intents.items()):
        _insert_task_intent(connection, task_id, intent)
    for task_id, state in sorted(replay_state.task_states.items()):
        _insert_task_state(connection, task_id, state)
    for task_id, journal in sorted(replay_state.task_journal.items()):
        _insert_task_journal(connection, task_id, journal)
    for task_id, activity in sorted(replay_state.task_activity.items()):
        _insert_task_activity(connection, task_id, activity)
    for report in replay_state.stage_reports:
        _insert_stage_report(connection, report)
    for report in replay_state.recovery_reports:
        _insert_recovery_report(connection, report)
    for entry in replay_state.task_audit_entries:
        _insert_task_audit_entry(connection, entry)
    for task_id, row in sorted(replay_state.pipeline_task_state.items()):
        _insert_pipeline_task_state(connection, task_id, row)
    for transition in replay_state.pipeline_transitions:
        _insert_pipeline_transition(connection, transition)
    for lifecycle in replay_state.pipeline_journal:
        _insert_pipeline_journal(connection, lifecycle)


def _workspace_state_payload(replay_state: _ReplayState) -> dict[str, Any]:
    payload = dict(replay_state.workspace_state or WorkspaceState().model_dump(mode="json"))
    payload["next_task_number"] = max(int(payload.get("next_task_number") or 0), _highest_task_number(replay_state))
    return payload


def _highest_task_number(replay_state: _ReplayState) -> int:
    highest = 0
    for task_id in set(replay_state.task_intents) | set(replay_state.task_states):
        try:
            highest = max(highest, int(str(task_id).removeprefix("T-")))
        except ValueError:
            continue
    return highest


def _insert_workspace_state(connection: sqlite3.Connection, payload: dict[str, Any]) -> None:
    now = utcnow()
    state_payload = WorkspaceState.model_validate(payload).model_dump(mode="json")
    queue_payload = json.dumps(state_payload.pop("queue"), sort_keys=True)
    connection.execute(
        """
        INSERT INTO pool_state (workspace_key, payload, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(workspace_key) DO UPDATE SET
            payload = excluded.payload,
            updated_at = excluded.updated_at
        """,
        ("workspace", json.dumps(state_payload, sort_keys=True), now),
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


def _insert_task_intent(connection: sqlite3.Connection, task_id: str, payload: dict[str, Any]) -> None:
    intent = TaskIntentRecord.model_validate(payload)
    updated_at = str(payload.get("updated_at") or intent.created_at or utcnow())
    column_values = _task_intent_column_values(intent)
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
            json.dumps(intent.model_dump(mode="json"), sort_keys=True),
            updated_at,
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


def _insert_task_state(connection: sqlite3.Connection, task_id: str, payload: dict[str, Any]) -> None:
    state = TaskStateRecord.model_validate(payload)
    updated_at = state.updated_at or utcnow()
    connection.execute(
        """
        INSERT INTO task_state (task_id, payload, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            payload = excluded.payload,
            updated_at = excluded.updated_at
        """,
        (task_id, json.dumps(state.model_dump(mode="json"), sort_keys=True), updated_at),
    )
    connection.execute(
        """
        UPDATE task_intent
        SET lifecycle_status = ?, pipeline_status = ?
        WHERE task_id = ?
        """,
        (str(state.status), str(state.pipeline_status), task_id),
    )


def _insert_task_journal(connection: sqlite3.Connection, task_id: str, journal: list[dict[str, Any]]) -> None:
    for fallback_index, entry in enumerate(journal):
        entry_index = int(entry.get("entry_index") if entry.get("entry_index") is not None else fallback_index)
        created_at = str(entry.get("created_at") or utcnow())
        message = str(entry.get("message") or "")
        metadata = entry.get("metadata")
        metadata_payload = metadata if isinstance(metadata, dict) else {}
        connection.execute(
            """
            INSERT OR REPLACE INTO task_journal (task_id, entry_index, created_at, message, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, entry_index, created_at, message, json.dumps(metadata_payload, sort_keys=True)),
        )


def _insert_task_activity(connection: sqlite3.Connection, task_id: str, activity: list[dict[str, Any]]) -> None:
    for entry_index, entry in enumerate(activity):
        created_at = str(entry.get("created_at") or utcnow())
        connection.execute(
            """
            INSERT INTO task_activity (task_id, entry_index, created_at, payload)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, entry_index, created_at, json.dumps(entry, sort_keys=True)),
        )


def _insert_stage_report(connection: sqlite3.Connection, report: dict[str, Any]) -> None:
    task_id = str(report.get("task_id") or "")
    pipeline_state = str(report.get("pipeline_state") or report.get("stage") or "")
    created_at = str(report.get("created_at") or utcnow())
    connection.execute(
        """
        INSERT INTO stage_reports (task_id, pipeline_state, created_at, payload)
        VALUES (?, ?, ?, ?)
        """,
        (task_id, pipeline_state, created_at, json.dumps(report, sort_keys=True)),
    )


def _insert_recovery_report(connection: sqlite3.Connection, report: dict[str, Any]) -> None:
    task_id = str(report.get("task_id") or "")
    origin_stage = _optional_str(report.get("origin_stage"))
    trigger = report.get("trigger_event_kind")
    if hasattr(trigger, "value"):
        trigger_value = str(trigger.value)
    else:
        trigger_value = str(trigger or "")
    created_at = str(report.get("created_at") or utcnow())
    connection.execute(
        """
        INSERT INTO recovery_reports (task_id, origin_stage, trigger_event_kind, created_at, payload)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, origin_stage, trigger_value, created_at, json.dumps(report, sort_keys=True)),
    )


def _insert_task_audit_entry(connection: sqlite3.Connection, entry: dict[str, Any]) -> None:
    connection.execute(
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
        (
            str(entry.get("task_id") or ""),
            str(entry.get("created_at") or utcnow()),
            str(entry.get("action") or ""),
            str(entry.get("actor") or ""),
            str(entry.get("source") or ""),
            _optional_str(entry.get("task_status_before")),
            _optional_str(entry.get("task_status_after")),
            _optional_str(entry.get("pipeline_status_before")),
            _optional_str(entry.get("pipeline_status_after")),
            _optional_int(entry.get("queue_position_before")),
            _optional_int(entry.get("queue_position_after")),
            json.dumps(dict(entry.get("context") or {}), sort_keys=True),
        ),
    )


def _insert_pipeline_task_state(connection: sqlite3.Connection, task_id: str, row: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO pipeline_task_state (task_id, stage, pipeline_mode, payload, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            stage = excluded.stage,
            pipeline_mode = excluded.pipeline_mode,
            payload = excluded.payload,
            updated_at = excluded.updated_at
        """,
        (
            task_id,
            str(row.get("stage") or ""),
            str(row.get("pipeline_mode") or "full"),
            json.dumps(dict(row.get("payload") or {}), sort_keys=True),
            str(row.get("updated_at") or utcnow()),
        ),
    )


def _insert_pipeline_transition(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO pipeline_transitions (
            task_id, seq, created_at,
            from_stage, event_type, event_payload,
            to_stage, rule_description, delta
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(row.get("task_id") or ""),
            int(row.get("seq") or 0),
            str(row.get("created_at") or utcnow()),
            str(row.get("from_stage") or ""),
            str(row.get("event_type") or ""),
            json.dumps(dict(row.get("event_payload") or {}), sort_keys=True),
            str(row.get("to_stage") or ""),
            str(row.get("rule_description") or ""),
            json.dumps(dict(row.get("delta") or {}), sort_keys=True),
        ),
    )


def _insert_pipeline_journal(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO pipeline_journal (task_id, seq, created_at, kind, payload)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            str(row.get("task_id") or ""),
            int(row.get("seq") or 0),
            str(row.get("created_at") or utcnow()),
            str(row.get("kind") or ""),
            json.dumps(dict(row.get("payload") or {}), sort_keys=True),
        ),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _task_intent_column_values(intent: TaskIntentRecord) -> dict[str, str]:
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
        "lifecycle_status": "queued",
        "pipeline_status": "backlog",
    }
