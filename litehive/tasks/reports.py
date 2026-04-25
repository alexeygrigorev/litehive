"""Recovery evidence, task activity, and report helpers."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from litehive.agents.session_store import load_subagent_artifacts
from litehive.db.schema import connect_workspace_db
from litehive.git.ops import GitError, current_head, is_git_repo, status_porcelain
from litehive.domain.recovery import TriggerEventKind
from litehive.domain.reports import (
    RecoveryAction,
    RecoveryEvidenceItem,
    RecoveryReport,
    StageReport,
    TaskActivityEntry,
    canonical_stage_report_verdict,
)
from litehive.domain.task import TaskRecord

from .activity import (
    append_task_activity,
    load_task_activity,
    resolve_task_activity_path,
)
from .paths import (
    latest_run_all_log_path,
    latest_subagent_base,
    resolve_artifact_path,
    status_entry_paths,
    task_dir,
)

RETRACTED_FILESYSTEM_MARKER = "[retracted - filesystem check shows no changes landed]"
_RETRACTABLE_STEPS = {"implementing", "testing", "accepting"}
_FILES_CHANGED_PLACEHOLDERS = {"none", "n/a", "-", ""}


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


def collect_recovery_evidence(
    root: Path,
    task: TaskRecord,
    *,
    stage: str | None = None,
) -> list[RecoveryEvidenceItem]:
    from litehive.observability.engine_monitoring import load_engine_monitoring

    from litehive.state.records import get_task_worktree_path
    from litehive.worktree import resolve_recorded_worktree_path

    evidence: list[RecoveryEvidenceItem] = []
    activity_path = resolve_task_activity_path(root, task)
    events_path = task_dir(root, task) / "events.jsonl"
    latest_report = latest_stage_report(root, task)
    latest_run_log = latest_run_all_log_path(root)
    monitoring = load_engine_monitoring(root)
    engine_name = (
        task.runtime.execution.active_subagent.engine
        if task.runtime.execution.active_subagent is not None
        else task.runtime.execution.last_subagent.engine
        if task.runtime.execution.last_subagent is not None
        else None
    )
    engine_record = monitoring.engines.get(engine_name or "")
    subagent_base = latest_subagent_base(root, task)

    evidence.append(
        RecoveryEvidenceItem(
            kind="task",
            label="task record",
            exists=True,
            summary=(
                f"status={task.status} pipeline_status={task.pipeline_status} priority={task.priority}"
                + (f" close_reason={task.close_reason}" if task.close_reason else "")
                + (f" flag_reason={task.flag_reason}" if task.flag_reason else "")
            ),
        )
    )
    evidence.append(
        RecoveryEvidenceItem(
            kind="runtime",
            label="runtime state",
            summary=(
                f"execution_status={task.runtime.pipeline.execution_status} current_stage={task.runtime.pipeline.current_stage.stage} "
                f"last_outcome={task.runtime.pipeline.last_outcome.kind or 'none'}"
            ),
        )
    )
    evidence.append(
        RecoveryEvidenceItem(
            kind="activity",
            label=activity_path.name,
            path=str(activity_path.relative_to(root)),
            exists=activity_path.exists(),
            summary=f"activity entries={len(load_task_activity(root, task))}",
        )
    )
    evidence.append(
        RecoveryEvidenceItem(
            kind="events",
            label="events.jsonl",
            path=str(events_path.relative_to(root)),
            exists=events_path.exists(),
            summary="task lifecycle and subagent event stream",
        )
    )
    if latest_report is not None:
        evidence.append(
            RecoveryEvidenceItem(
                kind="stage_report",
                label="latest stage report",
                exists=True,
                summary=_stage_report_context(latest_report),
                metadata={
                    "pipeline_state": latest_report.pipeline_state,
                    "verdict": latest_report.verdict,
                    "source": latest_report.source,
                    "created_at": latest_report.created_at,
                    "failure_classification": latest_report.failure_classification,
                },
            )
        )
    if subagent_base is not None:
        rel_subagent_path = str(subagent_base.relative_to(task_dir(root, task)))
        subagent_ref = next((ref for ref in task.subagents if ref.path == rel_subagent_path), None)
        artifacts = {} if subagent_ref is None else load_subagent_artifacts(root, task.id, subagent_ref.id)
        for key, label in (
            ("session", "latest subagent session"),
            ("report", "latest subagent report"),
            ("transcript.md", "latest subagent transcript"),
            ("stdout.txt", "latest subagent stdout"),
            ("stderr.txt", "latest subagent stderr"),
            ("timeline", "latest subagent events timeline"),
        ):
            path = None if key in {"session", "report", "timeline"} else resolve_artifact_path(subagent_base, key)
            exists = key in {"session", "report", "timeline"} and key in artifacts
            display_path = path if path is not None else subagent_base / key
            evidence.append(
                RecoveryEvidenceItem(
                    kind="subagent_artifact",
                    label=label,
                    path=str(display_path.relative_to(root)),
                    exists=exists or path is not None,
                    summary=f"artifact from {subagent_base.name}",
                )
            )
    if latest_run_log is not None:
        try:
            log_display_path = str(latest_run_log.relative_to(root))
        except ValueError:
            # Run-all logs live under ~/.local/share/litehive (out of tree) per T-0297.
            log_display_path = str(latest_run_log)
        evidence.append(
            RecoveryEvidenceItem(
                kind="wrapper_log",
                label="latest run-all log",
                path=log_display_path,
                exists=True,
                summary="latest daemon/run-all wrapper log",
            )
        )
    evidence.append(
        RecoveryEvidenceItem(
            kind="engine_monitoring",
            label="engine monitoring",
            exists=bool(monitoring.engines),
            summary=(
                "no engine record"
                if engine_record is None
                else (
                    f"engine={engine_record.engine} invocations={engine_record.invocation_count} "
                    f"failures={engine_record.failure_count} limits={engine_record.limit_event_count}"
                )
            ),
        )
    )

    if is_git_repo(root):
        worktree_path = resolve_recorded_worktree_path(root, task.runtime.pipeline.git.worktree_path or task.git.worktree_path)
        worktree_rel = get_task_worktree_path(task)
        try:
            root_status = status_porcelain(root)
        except GitError:
            root_status = []
        worktree_status: list[str] = []
        if worktree_path is not None and worktree_path.exists():
            try:
                worktree_status = status_porcelain(worktree_path)
            except GitError:
                worktree_status = []
        evidence.append(
            RecoveryEvidenceItem(
                kind="git",
                label="main checkout git state",
                exists=True,
                summary=f"head={current_head(root) or 'missing'} dirty={len(root_status)}",
                metadata={"dirty_paths": status_entry_paths(root_status)},
            )
        )
        evidence.append(
            RecoveryEvidenceItem(
                kind="worktree",
                label="task worktree state",
                path=worktree_rel,
                exists=worktree_path.exists() if worktree_path is not None else False,
                summary=("task worktree not configured" if worktree_path is None else f"dirty={len(worktree_status)}"),
                metadata={"dirty_paths": status_entry_paths(worktree_status), "stage": stage},
            )
        )
    return evidence


def _stage_report_context(report: StageReport) -> str:
    return f"{report.pipeline_state}/{report.verdict} source={report.source} summary={report.summary}"


def _insert_recovery_report(root: Path, task: TaskRecord, report: RecoveryReport) -> ReportReference:
    payload = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    with connect_workspace_db(root) as connection:
        cursor = connection.execute(
            """
            INSERT INTO recovery_reports (task_id, origin_stage, trigger_event_kind, created_at, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task.id, report.origin_stage, report.trigger_event_kind.value, report.created_at, payload),
        )
        connection.commit()
    return ReportReference(table="recovery_reports", row_id=int(cursor.lastrowid))


def record_recovery_report(
    root: Path,
    task: TaskRecord,
    *,
    trigger_event_kind: TriggerEventKind,
    origin_stage: str | None,
    summary: str,
    runnable_state: str,
    actions: list[RecoveryAction] | None = None,
    failure_classification: str | None = None,
    blocker: str | None = None,
    warnings: list[str] | None = None,
) -> ReportReference:
    report = RecoveryReport(
        task_id=task.id,
        origin_stage=origin_stage,
        trigger_event_kind=trigger_event_kind,
        summary=summary,
        failure_classification=failure_classification,
        runnable_state=runnable_state,  # type: ignore[arg-type]
        blocker=blocker,
        evidence=collect_recovery_evidence(root, task, stage=origin_stage),
        actions=list(actions or []),
        warnings=list(warnings or []),
    )
    ref = _insert_recovery_report(root, task, report)
    latest_report = latest_stage_report(root, task)
    append_task_activity(
        root,
        task,
        TaskActivityEntry(
            role="recovery",
            stage=origin_stage or task.pipeline_status,
            verdict="comment",
            message=(
                f"Recovery trigger `{trigger_event_kind.value}`: {summary}\n"
                f"runnable_state: {runnable_state}\n"
                f"report: {ref}"
                + (f"\nlatest_stage_report: {_stage_report_context(latest_report)}" if latest_report is not None else "")
                + (f"\nblocker: {blocker}" if blocker else "")
            ),
        ),
    )
    return ref


def append_activity_entry(root: Path, task: TaskRecord, entry: "TaskActivityEntry") -> None:
    append_task_activity(root, task, entry)


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
        connection.commit()
    return ReportReference(table="stage_reports", row_id=report_id)


def _deserialize_recovery_report_payload(payload: dict[str, object]) -> RecoveryReport:
    return RecoveryReport(**payload)


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
            reports.append(_deserialize_recovery_report_payload(payload))
        except ValidationError:
            continue
    return reports


def latest_recovery_report(root: Path, task: TaskRecord) -> RecoveryReport | None:
    reports = load_recovery_reports(root, task)
    return reports[-1] if reports else None


def _load_stage_reports(
    root: Path,
    *,
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


def load_stage_reports_for_task_id(
    root: Path,
    task_id: str,
    *,
    pipeline_state: str | None = None,
) -> list[StageReport]:
    return _load_stage_reports(root, task_id=task_id, pipeline_state=pipeline_state)


def load_workspace_stage_reports(root: Path) -> list[StageReport]:
    return _load_stage_reports(root)


def load_stage_reports(
    root: Path,
    task: TaskRecord,
    *,
    pipeline_state: str | None = None,
    stage: str | None = None,
) -> list[StageReport]:
    selected_pipeline_state = pipeline_state if pipeline_state is not None else stage
    return _load_stage_reports(root, task_id=task.id, pipeline_state=selected_pipeline_state)


def latest_stage_report(root: Path, task: TaskRecord, *, source: str | None = None) -> StageReport | None:
    reports = load_stage_reports(root, task)
    for report in reversed(reports):
        if source is not None and report.source != source:
            continue
        return report
    return None


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


def normalized_files_changed(paths: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        stripped = str(raw_path).strip().strip("/")
        if stripped.lower() in _FILES_CHANGED_PLACEHOLDERS or not stripped:
            continue
        if stripped in seen:
            continue
        seen.add(stripped)
        normalized.append(stripped)
    return normalized


def is_retracted_activity_entry(entry: "TaskActivityEntry") -> bool:
    return RETRACTED_FILESYSTEM_MARKER in entry.message


def is_retractable_pass_entry(entry: "TaskActivityEntry") -> bool:
    return (
        entry.verdict == "pass"
        and entry.stage in _RETRACTABLE_STEPS
        and bool(normalized_files_changed(entry.files_changed))
    )


def retract_activity_entry(entry: "TaskActivityEntry") -> bool:
    if is_retracted_activity_entry(entry):
        return False
    entry.message = f"{entry.message.rstrip()}\n{RETRACTED_FILESYSTEM_MARKER}"
    return True


def render_task_activity(root: Path, task: TaskRecord, *, for_prompt: bool = False) -> str:
    activity_entries = load_task_activity(root, task)
    if not activity_entries:
        return ""
    lines = ["Task activity:"]
    for entry in activity_entries:
        if for_prompt and is_retracted_activity_entry(entry):
            header = f"[{entry.created_at}] {entry.role} ({entry.stage}) - {entry.verdict} {RETRACTED_FILESYSTEM_MARKER}"
            lines.append(f"\n--- {header} ---")
            lines.append("Prior pass report withheld from prompt context after requeue-time filesystem validation.")
            claimed_files = normalized_files_changed(entry.files_changed)
            if claimed_files:
                lines.append(f"Claimed files: {', '.join(claimed_files)}")
            continue
        verdict_label = (
            f"{entry.verdict}; classification={entry.verdict_classification}"
            if entry.verdict_classification
            else str(entry.verdict)
        )
        header = f"[{entry.created_at}] {entry.role} ({entry.stage}) — {verdict_label}"
        lines.append(f"\n--- {header} ---")
        lines.append(entry.message)
        if entry.files_changed:
            lines.append(f"Files: {', '.join(entry.files_changed)}")
    return "\n".join(lines)
