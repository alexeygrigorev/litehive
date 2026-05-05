"""SQLite-backed storage boundary for stage and recovery reports."""

from dataclasses import dataclass
import json
from pathlib import Path

from pydantic import ValidationError

from litehive.db.schema import connect_workspace_db
from litehive.domain.reports import RecoveryReport, StageReport, canonical_stage_report_verdict
from litehive.domain.task import TaskRecord
from litehive.tasks.event_log import append_task_event


@dataclass(frozen=True, slots=True)
class ReportReference:
    table: str
    row_id: int

    def display(self) -> str:
        return f"sqlite:{self.table}/{self.row_id}"

    def relative_to(self, root: Path) -> str:
        del root
        return self.display()

    def __str__(self) -> str:
        return self.display()


def insert_recovery_report(root: Path, task: TaskRecord, report: RecoveryReport) -> ReportReference:
    payload = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    with connect_workspace_db(root) as connection:
        cursor = connection.execute(
            """
            INSERT INTO recovery_reports (task_id, origin_stage, trigger_event_kind, created_at, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task.id, report.origin_stage, report.trigger_event_kind.value, report.created_at, payload),
        )
        append_task_event(
            root,
            event_type="recovery_report_recorded",
            task_id=task.id,
            payload={"recovery_report": report.model_dump(mode="json")},
        )
        connection.commit()
    return ReportReference(table="recovery_reports", row_id=int(cursor.lastrowid))


def load_recovery_reports(root: Path, task: TaskRecord) -> list[RecoveryReport]:
    with connect_workspace_db(root) as connection:
        rows = connection.execute(
            """
            SELECT payload
            FROM recovery_reports
            WHERE task_id = ?
            ORDER BY id ASC
            """,
            (task.id,),
        ).fetchall()

    reports: list[RecoveryReport] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            reports.append(RecoveryReport(**payload))
        except ValidationError:
            continue
    return reports


def latest_recovery_report(root: Path, task: TaskRecord) -> RecoveryReport | None:
    reports = load_recovery_reports(root, task)
    return reports[-1] if reports else None


def record_stage_report(root: Path, task: TaskRecord, report: StageReport) -> ReportReference:
    payload = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    with connect_workspace_db(root) as connection:
        cursor = connection.execute(
            """
            INSERT INTO stage_reports (task_id, pipeline_state, created_at, payload)
            VALUES (?, ?, ?, ?)
            """,
            (task.id, report.pipeline_state, report.created_at, payload),
        )
        append_task_event(
            root,
            event_type="stage_report_recorded",
            task_id=task.id,
            payload={"stage_report": report.model_dump(mode="json")},
        )
        connection.commit()
    return ReportReference(table="stage_reports", row_id=int(cursor.lastrowid))


def rewrite_latest_stage_report(root: Path, task: TaskRecord, report: StageReport) -> ReportReference:
    payload = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    with connect_workspace_db(root) as connection:
        row = connection.execute(
            """
            SELECT id
            FROM stage_reports
            WHERE task_id = ? AND pipeline_state = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (task.id, report.pipeline_state),
        ).fetchone()
        if row is None:
            connection.commit()
            return record_stage_report(root, task, report)
        report_id = int(row["id"])
        connection.execute(
            """
            UPDATE stage_reports
            SET created_at = ?, payload = ?
            WHERE id = ?
            """,
            (report.created_at, payload, report_id),
        )
        append_task_event(
            root,
            event_type="stage_report_rewritten",
            task_id=task.id,
            payload={"rewritten_stage_report": report.model_dump(mode="json")},
        )
        connection.commit()
    return ReportReference(table="stage_reports", row_id=report_id)


def load_stage_reports_for_task_id(
    root: Path,
    task_id: str,
    pipeline_state: str | None = None,
) -> list[StageReport]:
    return _load_stage_reports(root, task_id=task_id, pipeline_state=pipeline_state)


def load_workspace_stage_reports(root: Path) -> list[StageReport]:
    return _load_stage_reports(root)


def load_stage_reports(
    root: Path,
    task: TaskRecord,
    pipeline_state: str | None = None,
    stage: str | None = None,
) -> list[StageReport]:
    selected_pipeline_state = pipeline_state if pipeline_state is not None else stage
    return _load_stage_reports(root, task_id=task.id, pipeline_state=selected_pipeline_state)


def latest_stage_report(root: Path, task: TaskRecord, source: str | None = None) -> StageReport | None:
    reports = load_stage_reports(root, task)
    for report in reversed(reports):
        if source is not None and report.source != source:
            continue
        return report
    return None


def _load_stage_reports(
    root: Path,
    task_id: str | None = None,
    pipeline_state: str | None = None,
) -> list[StageReport]:
    query = """
        SELECT payload
        FROM stage_reports
    """
    clauses: list[str] = []
    params: list[str] = []
    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(task_id)
    if pipeline_state is not None:
        clauses.append("pipeline_state = ?")
        params.append(pipeline_state)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id ASC"
    with connect_workspace_db(root) as connection:
        rows = connection.execute(query, tuple(params)).fetchall()

    reports: list[StageReport] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            reports.append(_deserialize_stage_report_payload(payload))
        except ValidationError:
            continue
    return reports


def _deserialize_stage_report_payload(payload: dict[str, object]) -> StageReport:
    normalized = dict(payload)
    if "pipeline_state" not in normalized and "stage" in normalized:
        normalized["pipeline_state"] = normalized["stage"]
    normalized.pop("stage", None)
    normalized.pop("files_changed", None)
    if isinstance(normalized.get("verdict"), str):
        verdict = canonical_stage_report_verdict(str(normalized["verdict"]))
        if verdict is not None:
            normalized["verdict"] = verdict
    return StageReport(**normalized)
