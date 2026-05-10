"""SQLite-backed storage boundary for stage and recovery reports."""

from dataclasses import dataclass
import json

from pydantic import ValidationError

from litehive.domain.reports import RecoveryReport, StageReport, canonical_stage_report_verdict
from litehive.domain.task import TaskRecord
from litehive.tasks.event_log import TaskEventLog
from litehive.workspace import Workspace


@dataclass(frozen=True, slots=True)
class ReportReference:
    """
    Opaque pointer to a stored report row.

    Callers can log or print this without leaking SQLite internals; the
    ``relative_to``/``display`` split exists so old file-path-based call
    sites keep working after the storage move from JSON files to SQLite.
    """

    table: str
    """SQLite table that holds the report row (stage_reports or recovery_reports)."""
    row_id: int
    """Auto-incremented primary key of the stored row."""

    def display(self) -> str:
        """
        Render the reference as the canonical ``sqlite:table/id`` token.

        The token shape journals, audit logs, and operator output all share
        so a printed reference is searchable across the workspace's logs
        without any further parsing.
        """
        return f"sqlite:{self.table}/{self.row_id}"

    def __str__(self) -> str:
        """
        Render the reference using the canonical display token.

        Used by f-strings and ``print`` so log lines emit the same form
        whether the caller went through ``display()`` or stringification.
        """
        return self.display()


class TaskReportStore:
    """
    Workspace-bound storage boundary for stage and recovery reports.

    The store owns SQLite report rows plus the matching task-event writes so
    callers do not thread raw workspace storage helpers through report flows.
    """

    def __init__(self, workspace: Workspace) -> None:
        """
        Bind the report store to a workspace's SQLite database.

        The store writes report rows and matching task-event entries through
        the workspace's connection, so callers never manage raw SQL.
        """
        self.workspace = workspace

    def insert_recovery_report(self, task: TaskRecord, report: RecoveryReport) -> ReportReference:
        """
        Persist a recovery report and emit the matching task event.
        """
        payload = json.dumps(report.model_dump(mode="json"), sort_keys=True)
        with self.workspace.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO recovery_reports (task_id, origin_stage, trigger_event_kind, created_at, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task.id, report.origin_stage, report.trigger_event_kind.value, report.created_at, payload),
            )
            TaskEventLog(self.workspace).append(
                event_type="recovery_report_recorded",
                task_id=task.id,
                payload={"recovery_report": report.model_dump(mode="json")},
            )
            connection.commit()
        return ReportReference(table="recovery_reports", row_id=int(cursor.lastrowid or 0))

    def load_recovery_reports(self, task: TaskRecord) -> list[RecoveryReport]:
        """
        Return every recovery report for a task in insertion order.
        """
        with self.workspace.connect() as connection:
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

    def latest_recovery_report(self, task: TaskRecord) -> RecoveryReport | None:
        """
        Return the most recent recovery report for a task.
        """
        reports = self.load_recovery_reports(task)
        if reports:
            return reports[-1]
        return None

    def record_stage_report(self, task: TaskRecord, report: StageReport) -> ReportReference:
        """
        Append a stage report row and emit the matching event.
        """
        payload = json.dumps(report.model_dump(mode="json"), sort_keys=True)
        with self.workspace.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO stage_reports (task_id, pipeline_state, created_at, payload)
                VALUES (?, ?, ?, ?)
                """,
                (task.id, report.pipeline_state, report.created_at, payload),
            )
            TaskEventLog(self.workspace).append(
                event_type="stage_report_recorded",
                task_id=task.id,
                payload={"stage_report": report.model_dump(mode="json")},
            )
            connection.commit()
        return ReportReference(table="stage_reports", row_id=int(cursor.lastrowid or 0))

    def rewrite_latest_stage_report(self, task: TaskRecord, report: StageReport) -> ReportReference:
        """
        Update the most recent stage report for a pipeline state in place.
        """
        payload = json.dumps(report.model_dump(mode="json"), sort_keys=True)
        with self.workspace.connect() as connection:
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
                return self.record_stage_report(task, report)
            report_id = int(row["id"])
            connection.execute(
                """
                UPDATE stage_reports
                SET created_at = ?, payload = ?
                WHERE id = ?
                """,
                (report.created_at, payload, report_id),
            )
            TaskEventLog(self.workspace).append(
                event_type="stage_report_rewritten",
                task_id=task.id,
                payload={"rewritten_stage_report": report.model_dump(mode="json")},
            )
            connection.commit()
        return ReportReference(table="stage_reports", row_id=report_id)

    def load_stage_reports_for_task_id(
        self,
        task_id: str,
        pipeline_state: str | None = None,
    ) -> list[StageReport]:
        """
        Load stage reports for a task when only the id is available.
        """
        return self._load_stage_reports(task_id=task_id, pipeline_state=pipeline_state)

    def load_workspace_stage_reports(self) -> list[StageReport]:
        """
        Load every stage report across the workspace in insertion order.
        """
        return self._load_stage_reports()

    def load_stage_reports(
        self,
        task: TaskRecord,
        pipeline_state: str | None = None,
        stage: str | None = None,
    ) -> list[StageReport]:
        """
        Load a task's stage reports filtered by pipeline state.
        """
        if pipeline_state is not None:
            selected_pipeline_state = pipeline_state
        else:
            selected_pipeline_state = stage
        return self._load_stage_reports(task_id=task.id, pipeline_state=selected_pipeline_state)

    def latest_stage_report(self, task: TaskRecord, source: str | None = None) -> StageReport | None:
        """
        Return the most recent stage report for a task.
        """
        reports = self.load_stage_reports(task)
        for report in reversed(reports):
            if source is not None and report.source != source:
                continue
            return report
        return None

    def _load_stage_reports(
        self,
        task_id: str | None = None,
        pipeline_state: str | None = None,
    ) -> list[StageReport]:
        """
        Single SQL query backing every stage-report loader.
        """
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
        with self.workspace.connect() as connection:
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
    """
    Coerce a stored stage-report payload into the current ``StageReport`` shape.

    Absorbs the legacy ``stage``/``files_changed`` fields and canonicalises
    the verdict string so older rows keep validating after schema renames;
    centralising the migration here means a new rename only needs editing
    in one place.
    """
    normalized = dict(payload)
    if "pipeline_state" not in normalized and "stage" in normalized:
        normalized["pipeline_state"] = normalized["stage"]
    normalized.pop("stage", None)
    normalized.pop("files_changed", None)
    if isinstance(normalized.get("verdict"), str):
        verdict = canonical_stage_report_verdict(str(normalized["verdict"]))
        if verdict is not None:
            normalized["verdict"] = verdict
    return StageReport.model_validate(normalized)
