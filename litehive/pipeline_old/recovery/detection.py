"""Recovery detection helpers shared by workspace and execution recovery."""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml

from litehive.config import LitehiveConfig, load_config
from litehive.git import GitError, checkpoint_message, find_commit_by_subject, is_git_repo
from litehive.models import StageReport, TaskRecord, WorkspaceState
from litehive.observability.events import last_event_timestamp
from litehive.tasks.paths import _latest_subagent_base, _read_text_artifact, _resolve_artifact_path, task_dir

_RECOVERY_ARTIFACT_TEXT_LIMIT = 4000
_RECOVERY_REPORT_ATTEMPT_LINE_LIMIT = 8


def _path_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_stranded_commit_task(task: TaskRecord) -> bool:
    return (
        task.pipeline_status == "done"
        and task.git.commit_sha is None
        and task.git.checkpoint_attempts > 0
    )


def _is_orphaned_commit_stage_task(task: TaskRecord, state: WorkspaceState) -> bool:
    return (
        task.pipeline_status == "commit_to_git"
        and task.status in {"queued", "in_progress", "interrupted"}
        and state.active_task_id != task.id
        and task.id not in state.queue
    )


def _should_requeue_commit_stage_task(task: TaskRecord) -> bool:
    return task.pipeline_status == "commit_to_git" and task.status in {
        "queued",
        "in_progress",
        "interrupted",
    }


def _report_verdict(path: Path) -> str | None:
    try:
        report = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    verdict = str(report.get("verdict") or "").strip().lower()
    return verdict or None


def _latest_stage_report_verdict(root: Path, task: TaskRecord) -> str | None:
    reports_dir = task_dir(root, task) / "reports"
    if not reports_dir.exists():
        return None
    reports = sorted(reports_dir.glob("*.yaml"))
    if not reports:
        return None
    return _report_verdict(reports[-1])


def _latest_stage_report_verdict_for_step(root: Path, task: TaskRecord, step: str) -> str | None:
    reports_dir = task_dir(root, task) / "reports"
    if not reports_dir.exists():
        return None
    reports = sorted(reports_dir.glob(f"{step}-*.yaml"))
    if not reports:
        return None
    return _report_verdict(reports[-1])


def _should_recover_flagged_commit_stage_task(root: Path, task: TaskRecord) -> bool:
    if task.pipeline_status != "commit_to_git" or task.status != "flagged":
        return False
    if task.git.commit_sha is not None or task.git.merge_agent_attempts >= 1:
        return False
    if _latest_stage_report_verdict_for_step(root, task, "accepting") in {"pass", "accept"}:
        return True
    if _latest_stage_report_verdict(root, task) in {"pass", "accept"}:
        return True
    return _latest_stage_report_verdict_for_step(root, task, "testing") in {"pass", "accept"}


def _should_resume_done_task_at_commit_stage(root: Path, task: TaskRecord) -> bool:
    if task.status != "done" or task.pipeline_status != "done" or task.git.commit_sha is not None:
        return False
    config = load_config(root)
    if not config.auto_commit or not task.git.auto_commit:
        return False
    return _latest_stage_report_verdict_for_step(root, task, "accepting") in {"pass", "accept"}


def _find_existing_checkpoint_commit(root: Path, task: TaskRecord) -> str | None:
    if not is_git_repo(root):
        return None
    try:
        return find_commit_by_subject(
            root,
            checkpoint_message(task, attempt=task.git.checkpoint_attempts),
        )
    except GitError:
        return None


def _has_inactive_running_tasks(
    root: Path,
    tasks_by_id: dict[str, TaskRecord],
    timeout_seconds: float,
) -> bool:
    for task in tasks_by_id.values():
        if task.runtime.execution_status != "running":
            continue
        ts_str = last_event_timestamp(root, task)
        if ts_str is None:
            continue
        try:
            event_time = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=UTC)
        if (datetime.now(UTC) - event_time).total_seconds() > timeout_seconds:
            return True
    return False


def _traceback_text(failed_report: StageReport) -> str:
    traceback_text = failed_report.failure_diagnostics.get("traceback")
    if isinstance(traceback_text, str) and traceback_text.strip():
        return traceback_text
    feedback = failed_report.feedback or ""
    return feedback if "Traceback" in feedback else ""


def _traceback_frame_paths(traceback_text: str) -> list[Path]:
    return [Path(match) for match in re.findall(r'File "([^"]+)"', traceback_text)]


def _traceback_fingerprint(traceback_text: str, summary: str) -> str:
    signature_lines = [
        line.strip()
        for line in traceback_text.splitlines()
        if line.strip().startswith('File "')
        or line.strip().startswith(
            ("raise ", "AssertionError", "RuntimeError", "ValueError", "TypeError")
        )
    ]
    payload = "\n".join(signature_lines) or summary
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _truncate_recovery_text(text: str, *, limit: int = _RECOVERY_ARTIFACT_TEXT_LIMIT) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit].rstrip()}\n...[truncated]..."


def _read_session_exit_code(session_text: str) -> str | int | None:
    if not session_text:
        return None
    try:
        session_payload = yaml.safe_load(session_text) or {}
    except yaml.YAMLError:
        session_payload = {}
    if isinstance(session_payload, dict):
        return session_payload.get("exit_code")
    return None


def _collect_report_attempt_lines(artifact_texts: tuple[tuple[str, str], ...]) -> list[str]:
    report_attempt_lines: list[str] = []
    for label, text in artifact_texts:
        for raw_line in text.splitlines():
            if "litehive report" not in raw_line:
                continue
            report_attempt_lines.append(f"{label}: {raw_line.strip()}")
            if len(report_attempt_lines) >= _RECOVERY_REPORT_ATTEMPT_LINE_LIMIT:
                return report_attempt_lines
    return report_attempt_lines


def _format_report_attempt_summary(report_attempt_lines: list[str]) -> str:
    if not report_attempt_lines:
        return "No explicit `litehive report` command was found in the captured artifacts."
    return "Found `litehive report` clues:\n" + "\n".join(
        f"- {line}" for line in report_attempt_lines
    )


def _load_failed_subagent_diagnostics(root: Path, task: TaskRecord) -> tuple[str, str]:
    base = _latest_subagent_base(root, task)
    if base is None:
        missing = "No failed subagent artifact directory was found for this task."
        return missing, missing

    def read_optional(name: str) -> str:
        path = _resolve_artifact_path(base, name)
        return _read_text_artifact(path) if path is not None else ""

    session_text = read_optional("session.yaml")
    stdout_text = read_optional("stdout.txt")
    stderr_text = read_optional("stderr.txt")
    transcript_text = read_optional("transcript.md")
    prompt_text = read_optional("prompt.txt")
    exit_code = _read_session_exit_code(session_text)
    artifact_texts = (
        ("prompt", prompt_text),
        ("stdout", stdout_text),
        ("stderr", stderr_text),
        ("transcript", transcript_text),
        ("session", session_text),
    )
    report_attempt_summary = _format_report_attempt_summary(
        _collect_report_attempt_lines(artifact_texts)
    )
    diagnostic_sections = [
        f"Failed subagent artifact base: {base.relative_to(root)}",
        f"Failed subagent exit code: {exit_code if exit_code is not None else 'unknown'}",
        report_attempt_summary,
        "",
        "Failed subagent session.yaml:",
        _truncate_recovery_text(artifact_texts[4][1] or "(missing)"),
        "",
        "Failed subagent stdout:",
        _truncate_recovery_text(artifact_texts[1][1] or "(missing)"),
        "",
        "Failed subagent stderr:",
        _truncate_recovery_text(artifact_texts[2][1] or "(missing)"),
        "",
        "Failed subagent transcript:",
        _truncate_recovery_text(artifact_texts[3][1] or "(missing)"),
    ]
    return "\n".join(diagnostic_sections), report_attempt_summary


def _classify_recovery_failure_owner(
    root: Path,
    failed_report: StageReport,
    *,
    config: LitehiveConfig | None,
) -> tuple[str, str, Path | None]:
    traceback_text = _traceback_text(failed_report)
    if not traceback_text:
        return "unknown", "", None
    frame_paths = _traceback_frame_paths(traceback_text)
    source_root = None
    if config and config.litehive_source_path:
        source_root = Path(config.litehive_source_path).expanduser().resolve()
    for frame in frame_paths:
        if source_root is not None and _path_within(frame, source_root):
            return "litehive", traceback_text, source_root
        if _path_within(frame, root):
            return "project", traceback_text, source_root
        normalized = frame.as_posix()
        if (
            "/site-packages/litehive/" in normalized
            or normalized.endswith("/litehive/__init__.py")
            or "/litehive/" in normalized
        ):
            return "litehive", traceback_text, source_root
    return "unknown", traceback_text, source_root


def _resolve_recovery_execution_root(root: Path, source_root: Path | None) -> Path | None:
    if source_root is not None and source_root.is_dir():
        return source_root
    if source_root is None:
        return root
    if (root / "litehive").is_dir():
        return root
    return None
