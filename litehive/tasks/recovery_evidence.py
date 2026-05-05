"""Recovery evidence collection for task recovery reports."""

from pathlib import Path

from litehive.agents.execution_trace import load_subagent_execution_trace
from litehive.agents.session_store import load_subagent_artifacts, load_subagent_event_stream
from litehive.domain.reports import RecoveryEvidenceItem, StageReport
from litehive.domain.task import TaskRecord
from litehive.git.ops import GitError, current_head, is_git_repo, status_porcelain
from litehive.observability.engine_monitoring import load_engine_monitoring
from litehive.observability.events import read_events
from litehive.state.records import get_task_worktree_path
from litehive.tasks.activity import load_task_activity
from litehive.tasks.paths import (
    latest_run_all_log_path,
    latest_subagent_base,
    resolve_artifact_path,
    status_entry_paths,
    task_dir,
)
from litehive.tasks.report_storage import latest_stage_report
from litehive.worktree import resolve_recorded_worktree_path


def collect_recovery_evidence(
    root: Path,
    task: TaskRecord,
    stage: str | None = None,
) -> list[RecoveryEvidenceItem]:
    """Gather every signal the recovery agent needs into a single evidence list — task record, runtime state, sqlite activity/events, the latest stage report, the latest subagent's artifacts, the wrapper log, engine monitoring, and the dirty-path snapshot for both the main checkout and the task worktree — so the recovery prompt sees the same facts the operator would when triaging by hand."""
    evidence: list[RecoveryEvidenceItem] = []
    activity_entries = load_task_activity(root, task)
    task_events = read_events(root, task)
    latest_report = latest_stage_report(root, task)
    latest_run_log = latest_run_all_log_path(root)
    monitoring = load_engine_monitoring(root)
    engine_name = None
    if task.runtime.execution.active_subagent is not None:
        engine_name = task.runtime.execution.active_subagent.engine
    elif task.subagents:
        engine_name = task.subagents[-1].engine
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
            label="sqlite activity",
            exists=bool(activity_entries),
            summary=f"activity entries={len(activity_entries)}",
        )
    )
    evidence.append(
        RecoveryEvidenceItem(
            kind="events",
            label="sqlite events",
            exists=bool(task_events),
            summary=f"task lifecycle and subagent event stream entries={len(task_events)}",
        )
    )
    if latest_report is not None:
        evidence.append(
            RecoveryEvidenceItem(
                kind="stage_report",
                label="latest stage report",
                exists=True,
                summary=stage_report_context(latest_report),
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
        event_stream = {} if subagent_ref is None else load_subagent_event_stream(root, task.id, subagent_ref.id)
        runtime_state = None
        if subagent_ref is not None:
            active_subagent = task.runtime.execution.active_subagent
            interruption = task.runtime.execution.interruption
            interrupted_subagent = None if interruption is None else interruption.subagent
            for state in (active_subagent, interrupted_subagent):
                if state is not None and state.id == subagent_ref.id:
                    runtime_state = state
                    break
        trace_view = (
            None
            if subagent_ref is None
            else load_subagent_execution_trace(
                root,
                task,
                subagent_ref,
                active=runtime_state is not None and runtime_state.status == "running",
                runtime_state=runtime_state,
            )
        )
        structured_artifact_keys = {"session", "report", "event_stream"}
        for key, label in (
            ("session", "latest subagent session"),
            ("report", "latest subagent report"),
            ("execution_trace", "latest subagent execution trace"),
            ("stdout.txt", "latest subagent stdout"),
            ("stderr.txt", "latest subagent stderr"),
            ("event_stream", "latest subagent event stream"),
        ):
            path = (
                trace_view.source
                if key == "execution_trace" and isinstance(trace_view.source if trace_view else None, Path)
                else None
                if key in structured_artifact_keys or key == "execution_trace"
                else resolve_artifact_path(subagent_base, key)
            )
            exists = (
                bool(event_stream)
                if key == "event_stream"
                else key in structured_artifact_keys
                and key in artifacts
                or key == "execution_trace"
                and trace_view is not None
            )
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
        worktree_path = resolve_recorded_worktree_path(
            root, task.runtime.pipeline.git.worktree_path or task.git.worktree_path
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
                summary=("task worktree not configured" if worktree_path is None else f"dirty={len(worktree_status)}"),
                metadata={"dirty_paths": status_entry_paths(worktree_status), "stage": stage},
            )
        )
    return evidence


def stage_report_context(report: StageReport) -> str:
    return f"{report.pipeline_state}/{report.verdict} source={report.source} summary={report.summary}"
