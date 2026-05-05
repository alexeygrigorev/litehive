"""Per-task summary helpers used by status renderers.

These helpers resolve display labels (engine, stage, verdict, summary), format
durations, and produce the multi-line per-task block that the CLI task list and
``litehive task show`` print. Pure formatting / data lookup; no IO beyond
read-only report storage.
"""

import sqlite3
from datetime import UTC, datetime
from typing import Any

from litehive.domain.common import TaskStage, TaskStatus
from litehive.domain.reports import ExecutionEstimate
from litehive.domain.task import TaskRecord
from litehive.tasks.report_storage import load_workspace_stage_reports
from litehive.workspace import Workspace

# Ordered pipeline stages for remaining-time estimation.
_PIPELINE_STAGES: list[TaskStage] = [
    TaskStage.GROOMING,
    TaskStage.IMPLEMENTING,
    TaskStage.TESTING,
    TaskStage.ACCEPTING,
    TaskStage.COMMIT_TO_GIT,
]


def estimate_task_execution(workspace: Workspace, task: TaskRecord) -> ExecutionEstimate:
    """Return velocity and ETA estimate based on workspace report history."""
    durations = _collect_report_durations(workspace)
    if not durations:
        return ExecutionEstimate()

    avg_duration = sum(durations) / len(durations)
    if avg_duration > 0:
        velocity = 3600.0 / avg_duration
    else:
        velocity = 0.0

    current_step = task.runtime.pipeline.current_stage.stage or task.pipeline_status or _PIPELINE_STAGES[0]
    try:
        current_idx = next(i for i, stage in enumerate(_PIPELINE_STAGES) if stage == current_step)
    except StopIteration:
        current_idx = 0
    remaining_stages = len(_PIPELINE_STAGES) - current_idx
    remaining_seconds = remaining_stages * avg_duration

    return ExecutionEstimate(
        stage_duration_seconds=avg_duration,
        remaining_seconds=remaining_seconds,
        velocity_stages_per_hour=velocity,
    )


def _collect_report_durations(workspace: Workspace) -> list[float]:
    """Collect positive stage report durations from workspace runtime storage."""
    return [
        float(report.duration_seconds)
        for report in load_workspace_stage_reports(workspace)
        if report.duration_seconds > 0
    ]


def _task_engine_label(task: TaskRecord, default_engine: str) -> str:
    """Resolve which engine name to display for a task, preferring live execution over historical record.

    Status surfaces want the engine *currently doing the work* — the live subagent if one
    is running, otherwise the most recent historical subagent, otherwise the workspace
    default. The fallback chain keeps display honest across idle/active/never-run states.
    """
    if task.runtime.execution.active_subagent is not None:
        return task.runtime.execution.active_subagent.engine
    subagents = getattr(task, "subagents", [])
    if subagents:
        return subagents[-1].engine
    return default_engine


def _task_stage_label(task: TaskRecord) -> str:
    """Pick a non-empty stage label for status output, falling back to pipeline status then ``-``.

    Tasks transitioning between stages can have an empty ``current_stage.stage`` for a brief
    window; the fallback to ``pipeline_status`` keeps the dashboard from rendering blank
    cells during that window without lying about the state.
    """
    return task.runtime.pipeline.current_stage.stage or task.pipeline_status or "-"


def _latest_stage_report_for_task(workspace: Workspace, task: TaskRecord) -> Any | None:
    """Fetch the most recent stage report for a task, swallowing storage errors so status never raises.

    Status output is best-effort: a missing or corrupt report row must not break the CLI
    render. Callers that want hard failures should go through ``report_storage`` directly;
    this wrapper exists specifically so the dashboard renderers can ignore IO/parse errors.
    """
    try:
        from litehive.tasks.report_storage import latest_stage_report  # noqa: PLC0415

        return latest_stage_report(workspace, task)
    except (OSError, sqlite3.DatabaseError, ValueError):
        return None


def _task_last_verdict_label(task: TaskRecord, workspace: Workspace) -> str:
    """Resolve the verdict to display by preferring the persisted stage report over the in-memory outcome.

    The stage report is the durable record; the runtime ``last_outcome`` may lag or be
    cleared on certain transitions. Falling back through both keeps the health/flagged
    sections honest when one of the two has been pruned.
    """
    latest_report = _latest_stage_report_for_task(workspace, task)
    return (None if latest_report is None else latest_report.verdict) or task.runtime.pipeline.last_outcome.kind or "-"


def _task_last_summary_label(task: TaskRecord, workspace: Workspace) -> str:
    """Pick the most descriptive human-readable reason text available across reports, outcomes, and flags.

    Different tasks die in different ways: a stage report has a summary, a runtime outcome
    has a reason, a flagged task has a flag_reason, a closed task has a close_reason. The
    health and recent-completion renderers want one column for "why", so the fallback chain
    walks each source in priority order.
    """
    latest_report = _latest_stage_report_for_task(workspace, task)
    return (
        (None if latest_report is None else latest_report.summary)
        or task.runtime.pipeline.last_outcome.reason
        or task.flag_reason
        or task.close_reason
        or "-"
    )


def _latest_stage_failure_classification(workspace: Workspace, task: TaskRecord) -> str | None:
    """Surface the persisted-report failure classification for the task summary, or ``None`` if unavailable.

    The runtime outcome carries its own ``failure_classification``; the report row keeps an
    independent value so post-hoc reclassification (e.g. recovery agents) doesn't have to
    rewrite runtime state. Status prints both side-by-side, so the report-side accessor
    lives separately and tolerates missing rows.
    """
    try:
        from litehive.tasks.report_storage import latest_stage_report  # noqa: PLC0415

        report = latest_stage_report(workspace, task)
    except (OSError, sqlite3.DatabaseError, ValueError):
        return None
    if report is None:
        return None
    return report.failure_classification


def render_task_summary(task: TaskRecord, active: bool, workspace: Workspace) -> list[str]:
    """Build the multi-line per-task block used by ``litehive task list`` and ``litehive task show``.

    Called by the CLI task-listing handlers in ``litehive/cli/workspace.py``. The ``active``
    flag controls only the leading marker (``*`` vs blank); the rest of the output is a
    deterministic dump of pipeline state, retry policy, last outcome, failure history, and
    execution estimate so operators can read one block per task without cross-referencing.
    """
    if active:
        marker = "*"
    else:
        marker = " "
    retry_policy = task.retry_policy.max_retries
    if retry_policy is None:
        retry_label = "default"
    else:
        retry_label = str(retry_policy)
    lines = [f"{marker} {task.id} [{task.status}/{task.pipeline_status}] retry_limit={retry_label} {task.title}"]
    if task.depends_on:
        lines.append(f"  depends_on={', '.join(task.depends_on)}")
    if task.model:
        lines.append(f"  engine=workspace-default model={task.model or 'default'}")
    _wt_path = task.runtime.pipeline.git.worktree_path or task.git.worktree_path
    lines.append(f"  auto_commit={task.git.auto_commit}")
    if task.git.commit_message:
        lines.append(f"  commit_message={task.git.commit_message}")
    if task.status == TaskStatus.FLAGGED:
        lines.append(f"  flag_reason={task.flag_reason or 'unknown'}")
    if task.status in {TaskStatus.CLOSED, TaskStatus.DONE}:
        lines.append(f"  close_reason={task.close_reason or 'unknown'}")

    runtime = task.runtime
    latest_report = _latest_stage_report_for_task(workspace, task)
    if retry_policy is not None:
        configured_limit = retry_policy
    else:
        configured_limit = "default"
    lines.append(f"  retry_policy=configured:{configured_limit} effective:{runtime.pipeline.retry_limit}")
    if (
        runtime.pipeline.execution_status != "idle"
        or runtime.pipeline.current_stage.stage
        or runtime.pipeline.current_stage.status != "idle"
    ):
        parts = [f"run={runtime.pipeline.execution_status}"]
        parts.append(f"retries={runtime.pipeline.retry_count}/{runtime.pipeline.retry_limit}")
        if runtime.pipeline.run_started_at:
            parts.append(f"started={runtime.pipeline.run_started_at}")
        if runtime.pipeline.current_stage.status != "idle":
            parts.append(f"stage_status={runtime.pipeline.current_stage.status}")
        if runtime.pipeline.current_stage.stage:
            stage_duration = _duration_label(
                runtime.pipeline.current_stage.started_at, runtime.pipeline.current_stage.duration_seconds
            )
            parts.append(f"stage={runtime.pipeline.current_stage.stage}")
            parts.append(f"stage_duration={stage_duration}")
        lines.append("  " + " ".join(parts))

    if runtime.execution.active_subagent is not None:
        subagent_duration = _duration_label(runtime.execution.active_subagent.started_at, 0)
        pid_label = _pid_label(runtime.execution.active_subagent.pid)
        sandbox_label = _sandbox_label(
            runtime.execution.active_subagent.sandboxed,
            runtime.execution.active_subagent.sandbox_summary,
        )
        lines.append(
            "  "
            + (
                f"subagent={runtime.execution.active_subagent.id} {runtime.execution.active_subagent.role}/{runtime.execution.active_subagent.engine} "
                f"{runtime.execution.active_subagent.status} {pid_label} duration={subagent_duration} {sandbox_label}"
            )
        )
    if runtime.execution.last_engine_switch is not None:
        lines.append(
            "  "
            + (
                f"engine_switch={runtime.execution.last_engine_switch.stage} "
                f"{runtime.execution.last_engine_switch.from_engine}->{runtime.execution.last_engine_switch.to_engine} "
                f"reason={runtime.execution.last_engine_switch.reason}"
            )
        )

    if runtime.execution.interruption is not None:
        interruption = runtime.execution.interruption
        lines.append(
            "  "
            + (
                f"interruption={interruption.source} stage={interruption.stage or '-'} "
                f"resume_from={interruption.resume_stage or interruption.pipeline_status or '-'} "
                f"interrupted_at={interruption.interrupted_at or '-'} "
                f"detected_at={interruption.detected_at or '-'} "
                f"reason={interruption.reason or interruption.summary or '-'}"
            )
        )

    if latest_report is not None:
        summary = latest_report.summary or "-"
        lines.append(
            "  "
            + (
                f"last_report={latest_report.pipeline_state}/{latest_report.verdict} "
                f"duration={_seconds_label(latest_report.duration_seconds)} summary={summary}"
            )
        )
    report_classification = _latest_stage_failure_classification(workspace, task)
    if report_classification is not None:
        lines.append(f"  last_report_failure_classification={report_classification}")

    if runtime.pipeline.last_outcome.kind is not None:
        stage = runtime.pipeline.last_outcome.stage or "-"
        reason_code = runtime.pipeline.last_outcome.reason_code or "-"
        reason = runtime.pipeline.last_outcome.reason or "-"
        recorded_at = runtime.pipeline.last_outcome.recorded_at or "-"
        follow_up_task_id = runtime.pipeline.last_outcome.follow_up_task_id or "-"
        if runtime.pipeline.last_outcome.kind in {"closed", "done"}:
            outcome_label = "close_reason"
        else:
            outcome_label = "reason_code"
        lines.append(
            "  "
            + (
                f"outcome={runtime.pipeline.last_outcome.kind} stage={stage} "
                f"{outcome_label}={reason_code} recorded_at={recorded_at} "
                f"follow_up_task={follow_up_task_id} "
                f"retry_state={runtime.pipeline.last_outcome.retry_count}/{runtime.pipeline.last_outcome.retry_limit} "
                f"reason={reason}"
            )
        )
        if runtime.pipeline.last_outcome.failure_classification is not None:
            phase = runtime.pipeline.last_outcome.failure_diagnostics.get("phase", "-")
            lines.append(
                "  "
                + (
                    f"failure_classification={runtime.pipeline.last_outcome.failure_classification} failure_phase={phase}"
                )
            )

    if runtime.pipeline.failed_run_history:
        lines.append("  failed_run_history:")
        for key, record in sorted(runtime.pipeline.failed_run_history.items()):
            lines.append(
                "  "
                + (
                    f"  {key} stage={record.stage} shape={record.failure_shape} "
                    f"count={record.count} latest_at={record.latest_at or '-'} "
                    f"operator_override_count={record.operator_override_count}"
                )
            )

    if task.status == TaskStatus.FLAGGED and task.flag_reason == "merge_failed":
        wt_path = task.runtime.pipeline.git.worktree_path or task.git.worktree_path
        if wt_path:
            lines.append(f"  unmerged_worktree={wt_path}")
    if task.git.commit_sha:
        lines.append(f"  commit={task.git.commit_sha}")

    estimate = estimate_task_execution(workspace, task)
    if estimate.stage_duration_seconds > 0:
        lines.append(
            f"  stage_estimate={_seconds_label(int(estimate.stage_duration_seconds))} "
            f"velocity={estimate.velocity_stages_per_hour:.1f}stages/h "
            f"eta={_seconds_label(int(estimate.remaining_seconds))}"
        )

    return lines


def _duration_label(started_at: str | None, fallback_seconds: int) -> str:
    """Compute elapsed seconds from an ISO timestamp and format it, falling back when no start time exists.

    Status is rendered from frozen records that may predate the current process by hours;
    the live elapsed value is the useful one. When the start timestamp is missing or
    unparseable we fall back to the persisted ``duration_seconds`` rather than printing 0,
    so brief stages whose start time was never captured still show non-zero time.
    """
    if started_at is None:
        return _seconds_label(fallback_seconds)
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return _seconds_label(fallback_seconds)
    seconds = max(0, int((datetime.now(UTC) - started).total_seconds()))
    return _seconds_label(seconds)


def _seconds_label(seconds: int) -> str:
    """Format a duration as the compact ``Ns`` / ``NmMMs`` / ``NhMMm`` form used everywhere in status output."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{remaining_seconds:02d}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h{remaining_minutes:02d}m"


def _pid_label(pid: int | None) -> str:
    """Render ``pid=<n>`` for present PIDs and ``pid=-`` for missing ones, matching the rest of the status grammar."""
    if pid is not None:
        return f"pid={pid}"
    return "pid=-"


def _sandbox_label(sandboxed: bool, sandbox_summary: str) -> str:
    """Format the subagent sandbox column so operators can tell ``host`` from sandboxed runs at a glance."""
    if sandboxed:
        return f"sandbox={sandbox_summary or 'enabled'}"
    return "sandbox=host"
