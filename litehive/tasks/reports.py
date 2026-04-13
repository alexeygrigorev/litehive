"""Recovery evidence, task discussion comments, and report helpers."""

from pathlib import Path
from typing import Iterable

import yaml

from litehive.git.ops import GitError, current_head, is_git_repo, status_porcelain
from litehive.models import (
    RecoveryAction,
    RecoveryEvidenceItem,
    RecoveryReport,
    TaskRecord,
)

from .paths import (
    legacy_task_thread_file,
    latest_path,
    latest_run_all_log_path,
    latest_subagent_base,
    resolve_artifact_path,
    status_entry_paths,
    task_comments_file,
    task_dir,
    task_file,
    task_runtime_file,
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
    from litehive.observability import engine_monitoring_file, load_engine_monitoring

    from .crud import get_task_worktree_path
    from .worktrees import resolve_recorded_worktree_path

    evidence: list[RecoveryEvidenceItem] = []
    task_path = task_file(root, task)
    runtime_path = task_runtime_file(root, task)
    comments_path = task_comments_file(root, task)
    legacy_thread_path = legacy_task_thread_file(root, task)
    discussion_path = comments_path if comments_path.exists() or not legacy_thread_path.exists() else legacy_thread_path
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
            label="runtime.yaml",
            path=str(runtime_path.relative_to(root)),
            exists=runtime_path.exists(),
            summary=(
                f"execution_status={task.runtime.execution_status} current_stage={task.runtime.current_stage.step} "
                f"last_outcome={task.runtime.last_outcome.kind or 'none'}"
            ),
        )
    )
    evidence.append(
        RecoveryEvidenceItem(
            kind="thread",
            label="comments.yaml",
            path=str(discussion_path.relative_to(root)),
            exists=comments_path.exists() or legacy_thread_path.exists(),
            summary=f"discussion entries={len(load_task_thread(root, task))}",
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
        for name, label in (
            ("session.yaml", "latest subagent session"),
            ("report.yaml", "latest subagent report"),
            ("transcript.md", "latest subagent transcript"),
            ("stdout.txt", "latest subagent stdout"),
            ("stderr.txt", "latest subagent stderr"),
            ("timeline.yaml", "latest subagent events timeline"),
        ):
            path = resolve_artifact_path(subagent_base, name)
            display_path = path if path is not None else subagent_base / name
            evidence.append(
                RecoveryEvidenceItem(
                    kind="subagent_artifact",
                    label=label,
                    path=str(display_path.relative_to(root)),
                    exists=path is not None,
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
        worktree_path = resolve_recorded_worktree_path(
            root, task.runtime.git.worktree_path or task.git.worktree_path
        )
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
                summary=(
                    "task worktree not configured"
                    if worktree_path is None
                    else f"dirty={len(worktree_status)}"
                ),
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
    path.write_text(
        yaml.safe_dump(report.model_dump(mode="python"), sort_keys=False), encoding="utf-8"
    )
    return path


def record_recovery_report(
    root: Path,
    task: TaskRecord,
    *,
    trigger: str,
    stage: str | None,
    summary: str,
    runnable_state: str,
    actions: list[RecoveryAction] | None = None,
    failure_classification: str | None = None,
    blocker: str | None = None,
    warnings: list[str] | None = None,
    recovery_subagent_id: str | None = None,
    recovery_subagent_path: str | None = None,
) -> Path:
    from litehive.models import TaskThreadComment

    report = RecoveryReport(
        task_id=task.id,
        stage=stage,
        trigger=trigger,
        summary=summary,
        failure_classification=failure_classification,
        runnable_state=runnable_state,  # type: ignore[arg-type]
        blocker=blocker,
        evidence=collect_recovery_evidence(root, task, stage=stage),
        actions=list(actions or []),
        warnings=list(warnings or []),
        recovery_subagent_id=recovery_subagent_id,
        recovery_subagent_path=recovery_subagent_path,
    )
    path = write_recovery_report(root, task, report)
    append_thread_comment(
        root,
        task,
        TaskThreadComment(
            role="recovery",
            step=stage or task.pipeline_status,
            verdict="comment",
            message=(
                f"Recovery trigger `{trigger}`: {summary}\n"
                f"runnable_state: {runnable_state}\n"
                f"report: {path.relative_to(root)}" + (f"\nblocker: {blocker}" if blocker else "")
            ),
        ),
    )
    return path


def append_thread_comment(root: Path, task: TaskRecord, comment: "TaskThreadComment") -> None:
    path = task_comments_file(root, task)
    existing = [entry.model_dump(mode="python") for entry in load_task_thread(root, task)]
    existing.append(comment.model_dump(mode="python"))
    path.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")


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


def is_retracted_thread_comment(comment: "TaskThreadComment") -> bool:
    return RETRACTED_FILESYSTEM_MARKER in comment.message


def is_retractable_pass_comment(comment: "TaskThreadComment") -> bool:
    return (
        comment.verdict == "pass"
        and comment.step in _RETRACTABLE_STEPS
        and bool(normalized_files_changed(comment.files_changed))
    )


def retract_thread_comment(comment: "TaskThreadComment") -> bool:
    if is_retracted_thread_comment(comment):
        return False
    comment.message = f"{comment.message.rstrip()}\n{RETRACTED_FILESYSTEM_MARKER}"
    return True


def load_task_thread(root: Path, task: TaskRecord) -> list["TaskThreadComment"]:
    from litehive.models import TaskThreadComment

    path = task_comments_file(root, task)
    if not path.exists():
        path = legacy_task_thread_file(root, task)
    if not path.exists():
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        return []
    return [TaskThreadComment(**entry) for entry in loaded if isinstance(entry, dict)]


def save_task_thread(root: Path, task: TaskRecord, thread: list["TaskThreadComment"]) -> None:
    path = task_comments_file(root, task)
    path.write_text(
        yaml.safe_dump([comment.model_dump(mode="python") for comment in thread], sort_keys=False),
        encoding="utf-8",
    )


def render_task_thread(root: Path, task: TaskRecord, *, for_prompt: bool = False) -> str:
    thread = load_task_thread(root, task)
    if not thread:
        return ""
    lines = ["Discussion thread:"]
    for c in thread:
        if for_prompt and is_retracted_thread_comment(c):
            header = f"[{c.created_at}] {c.role} ({c.step}) - {c.verdict} {RETRACTED_FILESYSTEM_MARKER}"
            lines.append(f"\n--- {header} ---")
            lines.append(
                "Prior pass report withheld from prompt context after requeue-time filesystem validation."
            )
            claimed_files = normalized_files_changed(c.files_changed)
            if claimed_files:
                lines.append(f"Claimed files: {', '.join(claimed_files)}")
            continue
        header = f"[{c.created_at}] {c.role} ({c.step}) — {c.verdict}"
        lines.append(f"\n--- {header} ---")
        lines.append(c.message)
        if c.files_changed:
            lines.append(f"Files: {', '.join(c.files_changed)}")
    return "\n".join(lines)
