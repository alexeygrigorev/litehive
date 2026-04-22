"""Recovery evidence, task discussion comments, and report helpers."""

import json
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError
import yaml

from litehive.agents.session_store import load_subagent_artifacts
from litehive.db.schema import connect_workspace_db
from litehive.git.ops import GitError, current_head, is_git_repo, status_porcelain
from litehive.domain.recovery import TriggerEventKind
from litehive.domain.reports import RecoveryAction, RecoveryEvidenceItem, RecoveryReport, StageReport
from litehive.domain.task import TaskRecord

from .activity import (
    append_task_activity,
    load_task_activity,
    task_activity_path,
)
from .paths import (
    latest_path,
    latest_run_all_log_path,
    latest_subagent_base,
    resolve_artifact_path,
    status_entry_paths,
    task_dir,
    task_file,
    task_recovery_dir,
)

RETRACTED_FILESYSTEM_MARKER = "[retracted - filesystem check shows no changes landed]"
_RETRACTABLE_STEPS = {"implementing", "testing", "accepting"}
_FILES_CHANGED_PLACEHOLDERS = {"none", "n/a", "-", ""}


def collect_recovery_evidence(
    root: Path,
    task: TaskRecord,
    *,
    stage: str | None = None,
) -> list[RecoveryEvidenceItem]:
    from litehive.observability.engine_monitoring import engine_monitoring_file, load_engine_monitoring

    from litehive.state.records import get_task_worktree_path
    from litehive.worktree import resolve_recorded_worktree_path

    evidence: list[RecoveryEvidenceItem] = []
    task_path = task_file(root, task)
    comments_path = task_activity_path(root, task)
    events_path = task_dir(root, task) / "events.jsonl"
    latest_report_path = latest_path(sorted((task_dir(root, task) / "reports").glob("*.yaml")))
    latest_run_log = latest_run_all_log_path(root)
    monitoring_path = engine_monitoring_file(root)
    monitoring = load_engine_monitoring(root)
    engine_name = (
        task.runtime.active_subagent.engine
        if task.runtime.active_subagent is not None
        else task.runtime.last_subagent.engine
        if task.runtime.last_subagent is not None
        else None
    )
    engine_record = monitoring.engines.get(engine_name or "")
    subagent_base = latest_subagent_base(root, task)

    evidence.append(
        RecoveryEvidenceItem(
            kind="task",
            label="task.yaml",
            path=str(task_path.relative_to(root)),
            exists=task_path.exists(),
            summary=f"status={task.status} pipeline_status={task.pipeline_status} priority={task.priority}",
        )
    )
    evidence.append(
        RecoveryEvidenceItem(
            kind="runtime",
            label="runtime state",
            summary=(
                f"execution_status={task.runtime.execution_status} current_stage={task.runtime.current_stage.stage} "
                f"last_outcome={task.runtime.last_outcome.kind or 'none'}"
            ),
        )
    )
    evidence.append(
        RecoveryEvidenceItem(
            kind="thread",
            label="task activity",
            path=str(comments_path.relative_to(root)),
            exists=comments_path.exists(),
            summary=f"discussion entries={len(load_task_activity(root, task))}",
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
    if latest_report_path is not None:
        evidence.append(
            RecoveryEvidenceItem(
                kind="stage_report",
                label="latest stage report",
                path=str(latest_report_path.relative_to(root)),
                exists=True,
                summary=f"latest report for {task.id}",
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
            label="engine-monitoring.yaml",
            path=str(monitoring_path.relative_to(root)),
            exists=monitoring_path.exists(),
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
        worktree_path = resolve_recorded_worktree_path(root, task.runtime.git.worktree_path or task.git.worktree_path)
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


def write_recovery_report(root: Path, task: TaskRecord, report: RecoveryReport) -> Path:
    reports_dir = task_recovery_dir(root, task)
    reports_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(reports_dir.glob("recovery-*.yaml"))
    ordinal = len(existing) + 1
    path = reports_dir / f"recovery-{ordinal:03d}.yaml"
    path.write_text(yaml.safe_dump(report.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    return path


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
    recovery_subagent_id: str | None = None,
    recovery_subagent_path: str | None = None,
) -> Path:
    from litehive.domain.reports import TaskActivityEntry

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
        recovery_subagent_id=recovery_subagent_id,
        recovery_subagent_path=recovery_subagent_path,
    )
    path = write_recovery_report(root, task, report)
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
                f"report: {path.relative_to(root)}" + (f"\nblocker: {blocker}" if blocker else "")
            ),
        ),
    )
    return path


def append_activity_entry(root: Path, task: TaskRecord, comment: "TaskActivityEntry") -> None:
    append_task_activity(root, task, comment)


def load_task_thread(root: Path, task: TaskRecord) -> list["TaskActivityEntry"]:
    """Compatibility alias for callers that still treat task activity as a thread."""
    return load_task_activity(root, task)


def write_stage_report(root: Path, task: TaskRecord, report: StageReport) -> Path:
    reports_dir = task_dir(root, task) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(reports_dir.glob(f"{report.stage}-*.yaml"))
    ordinal = len(existing) + 1
    path = reports_dir / f"{report.stage}-{ordinal:03d}.yaml"
    path.write_text(yaml.safe_dump(report.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    return path


def record_stage_report(root: Path, task: TaskRecord, report: StageReport) -> Path:
    payload = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    with connect_workspace_db(root) as connection:
        connection.execute(
            """
            INSERT INTO stage_reports (task_id, stage, created_at, payload)
            VALUES (?, ?, ?, ?)
            """,
            (task.id, report.stage, report.created_at, payload),
        )
        connection.commit()
    return write_stage_report(root, task, report)


def rewrite_latest_stage_report(root: Path, task: TaskRecord, report: StageReport) -> Path:
    payload = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    rewritten = False
    with connect_workspace_db(root) as connection:
        rows = connection.execute(
            """
            SELECT id
            FROM stage_reports
            WHERE task_id = ? AND stage = ?
            ORDER BY id DESC
            """,
            (task.id, report.stage),
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                UPDATE stage_reports
                SET created_at = ?, payload = ?
                WHERE id = ?
                """,
                (report.created_at, payload, int(row["id"])),
            )
            connection.commit()
            rewritten = True
            break

    if not rewritten:
        return record_stage_report(root, task, report)

    reports_dir = task_dir(root, task) / "reports"
    report_path = latest_path(sorted(reports_dir.glob(f"{report.stage}-*.yaml")))
    if report_path is None:
        return write_stage_report(root, task, report)
    report_path.write_text(yaml.safe_dump(report.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    return report_path


def load_stage_reports(root: Path, task: TaskRecord, *, stage: str | None = None) -> list[StageReport]:
    query = """
        SELECT payload
        FROM stage_reports
        WHERE task_id = ?
    """
    params: list[str] = [task.id]
    if stage is not None:
        query += " AND stage = ?"
        params.append(stage)
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
            reports.append(StageReport(**payload))
        except ValidationError:
            continue
    return reports


def latest_stage_report(root: Path, task: TaskRecord, *, source: str | None = None) -> StageReport | None:
    reports = load_stage_reports(root, task)
    for report in reversed(reports):
        if source is not None and report.source != source:
            continue
        return report
    return None


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


def is_retracted_activity_entry(comment: "TaskActivityEntry") -> bool:
    return RETRACTED_FILESYSTEM_MARKER in comment.message


def is_retractable_pass_entry(comment: "TaskActivityEntry") -> bool:
    return (
        comment.verdict == "pass"
        and comment.stage in _RETRACTABLE_STEPS
        and bool(normalized_files_changed(comment.files_changed))
    )


def retract_activity_entry(comment: "TaskActivityEntry") -> bool:
    if is_retracted_activity_entry(comment):
        return False
    comment.message = f"{comment.message.rstrip()}\n{RETRACTED_FILESYSTEM_MARKER}"
    return True


def render_task_thread(root: Path, task: TaskRecord, *, for_prompt: bool = False) -> str:
    thread = load_task_activity(root, task)
    if not thread:
        return ""
    lines = ["Discussion thread:"]
    for c in thread:
        if for_prompt and is_retracted_activity_entry(c):
            header = f"[{c.created_at}] {c.role} ({c.stage}) - {c.verdict} {RETRACTED_FILESYSTEM_MARKER}"
            lines.append(f"\n--- {header} ---")
            lines.append("Prior pass report withheld from prompt context after requeue-time filesystem validation.")
            claimed_files = normalized_files_changed(c.files_changed)
            if claimed_files:
                lines.append(f"Claimed files: {', '.join(claimed_files)}")
            continue
        header = f"[{c.created_at}] {c.role} ({c.stage}) — {c.verdict}"
        lines.append(f"\n--- {header} ---")
        lines.append(c.message)
        if c.files_changed:
            lines.append(f"Files: {', '.join(c.files_changed)}")
    return "\n".join(lines)
