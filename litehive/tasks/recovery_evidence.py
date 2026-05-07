"""Recovery evidence collection for task recovery reports."""

from pathlib import Path

from litehive.agents.execution_trace import load_subagent_execution_trace
from litehive.agents.session_store import load_subagent_artifacts, load_subagent_event_stream
from litehive.domain.common import SubagentStatus
from litehive.domain.reports import RecoveryEvidenceItem, StageReport
from litehive.domain.task import TaskRecord
from litehive.git.ops import GitError, current_head, is_git_repo, status_porcelain
from litehive.observability.engine_monitoring import load_engine_monitoring
from litehive.observability.events import read_events
from litehive.state.records import get_task_worktree_path
from litehive.tasks.paths import (
    latest_run_all_log_path,
    latest_subagent_base,
    resolve_artifact_path,
    status_entry_paths,
    task_dir,
)
from litehive.tasks.report_storage import latest_stage_report
from litehive.worktree.paths import resolve_recorded_worktree_path
from litehive.workspace import Workspace


def collect_recovery_evidence(
    workspace: Workspace,
    task: TaskRecord,
    stage: str | None = None,
) -> list[RecoveryEvidenceItem]:
    """
    Gather every signal the recovery agent needs into one evidence list.

    Bundles the task record, runtime state, SQLite activity/events, the
    latest stage report, the latest subagent's artifacts, the wrapper log,
    engine monitoring, and the dirty-path snapshot for both the main
    checkout and the task worktree. Centralising the gather lets the
    recovery prompt see the same facts the operator would when triaging by
    hand.
    """
    evidence: list[RecoveryEvidenceItem] = []
    root = workspace.root
    activity_entries = workspace.task_activity(task).load()
    task_events = read_events(workspace, task)
    latest_report = latest_stage_report(workspace, task)
    latest_run_log = latest_run_all_log_path(root)
    monitoring = load_engine_monitoring(workspace)
    engine_name = None
    if task.runtime.execution.active_subagent is not None:
        engine_name = task.runtime.execution.active_subagent.engine
    elif task.subagents:
        engine_name = task.subagents[-1].engine
    engine_record = monitoring.engines.get(engine_name or "")
    subagent_base = latest_subagent_base(root, task)

    if task.close_reason:
        close_reason_part = f" close_reason={task.close_reason}"
    else:
        close_reason_part = ""
    if task.flag_reason:
        flag_reason_part = f" flag_reason={task.flag_reason}"
    else:
        flag_reason_part = ""
    evidence.append(
        RecoveryEvidenceItem(
            kind="task",
            label="task record",
            exists=True,
            summary=(
                f"status={task.status} pipeline_status={task.pipeline_status} priority={task.priority}"
                + close_reason_part
                + flag_reason_part
            ),
        )
    )
    evidence.append(
        RecoveryEvidenceItem(
            kind="runtime",
            label="runtime state",
            summary=(
                f"execution_status={task.runtime.pipeline.execution_status} current_stage={task.current_pipeline_stage} "
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
        if subagent_ref is None:
            artifacts: dict = {}
            event_stream: dict = {}
        else:
            artifacts = load_subagent_artifacts(workspace, task.id, subagent_ref.id)
            event_stream = load_subagent_event_stream(workspace, task.id, subagent_ref.id)
        runtime_state = None
        if subagent_ref is not None:
            active_subagent = task.runtime.execution.active_subagent
            interruption = task.runtime.execution.interruption
            if interruption is None:
                interrupted_subagent = None
            else:
                interrupted_subagent = interruption.subagent
            for state in (active_subagent, interrupted_subagent):
                if state is not None and state.id == subagent_ref.id:
                    runtime_state = state
                    break
        if subagent_ref is None:
            trace_view = None
        else:
            trace_view = load_subagent_execution_trace(
                workspace,
                task,
                subagent_ref,
                active=runtime_state is not None and runtime_state.status == SubagentStatus.RUNNING,
                runtime_state=runtime_state,
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
            if trace_view:
                trace_source_for_key = trace_view.source
            else:
                trace_source_for_key = None
            if key == "execution_trace" and isinstance(trace_source_for_key, Path):
                path = trace_source_for_key
            elif key in structured_artifact_keys or key == "execution_trace":
                path = None
            else:
                path = resolve_artifact_path(subagent_base, key)
            if key == "event_stream":
                exists = bool(event_stream)
            else:
                exists = (
                    key in structured_artifact_keys
                    and key in artifacts
                    or key == "execution_trace"
                    and trace_view is not None
                )
            if path is not None:
                display_path = path
            else:
                display_path = subagent_base / key
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
    if engine_record is None:
        engine_monitoring_summary = "no engine record"
    else:
        engine_monitoring_summary = (
            f"engine={engine_record.engine} invocations={engine_record.invocation_count} "
            f"failures={engine_record.failure_count} limits={engine_record.limit_event_count}"
        )
    evidence.append(
        RecoveryEvidenceItem(
            kind="engine_monitoring",
            label="engine monitoring",
            exists=bool(monitoring.engines),
            summary=engine_monitoring_summary,
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
        if worktree_path is not None:
            worktree_exists = worktree_path.exists()
            worktree_summary = f"dirty={len(worktree_status)}"
        else:
            worktree_exists = False
            worktree_summary = "task worktree not configured"
        evidence.append(
            RecoveryEvidenceItem(
                kind="worktree",
                label="task worktree state",
                path=worktree_rel,
                exists=worktree_exists,
                summary=worktree_summary,
                metadata={"dirty_paths": status_entry_paths(worktree_status), "stage": stage},
            )
        )
    return evidence


def stage_report_context(report: StageReport) -> str:
    """
    Render the one-line summary the recovery prompt embeds for a stage report.

    Lets the recovery agent decide whether to advance, retry, or rewind
    without expanding the full report payload — the inline form keeps the
    prompt budget low while still exposing pipeline_state/verdict/source.
    """
    return f"{report.pipeline_state}/{report.verdict} source={report.source} summary={report.summary}"
