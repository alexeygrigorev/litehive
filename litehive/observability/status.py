"""Shared task observability formatting helpers."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

from litehive.config.paths import workspace_path
from litehive.config.engine_models import active_engine_freezes
from litehive.config.model import LitehiveConfig
from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.reports import ExecutionEstimate
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import TaskRecord, WorkspaceState

if TYPE_CHECKING:
    from litehive.observability.status_diagnostics import StatusIssue

# Ordered pipeline stages for remaining-time estimation.
_PIPELINE_STAGES = ["grooming", "implementing", "testing", "accepting", "commit_to_git"]

StatusRenderMode = Literal["fast", "full"]


@dataclass(slots=True)
class TaskPipelineStatusData:
    root: Path
    config: LitehiveConfig
    state: WorkspaceState
    runner: RunnerStatusState
    monitoring: WorkspaceEngineMonitoring
    issues: list["StatusIssue"]
    active_task_id: str | None
    active_task: TaskRecord | None
    queue_head: str | None
    waiting_lines: list[str]
    fast_runner_status: str


def collect_task_pipeline_status(
    root: Path,
    *,
    read_only: bool = False,
) -> TaskPipelineStatusData:
    from litehive.attention import waiting_for_you_lines
    from litehive.observability.status_diagnostics import collect_status_snapshot
    from litehive.state.records import get_task

    resolved_root = root.resolve()
    snapshot = collect_status_snapshot(resolved_root)
    active_task_id = snapshot.runner.active_task_id or snapshot.state.active_task_id
    active_task = None if read_only or not active_task_id else get_task(resolved_root, active_task_id)
    return TaskPipelineStatusData(
        root=resolved_root,
        config=snapshot.config,
        state=snapshot.state,
        runner=snapshot.runner,
        monitoring=snapshot.monitoring,
        issues=snapshot.issues,
        active_task_id=active_task_id,
        active_task=active_task,
        queue_head=snapshot.state.queue[0] if snapshot.state.queue else None,
        waiting_lines=["attention_items: unavailable"] if read_only else waiting_for_you_lines(resolved_root),
        fast_runner_status=_fast_runner_state_label(resolved_root, snapshot.runner),
    )


def render_task_pipeline_status_lines(
    status: TaskPipelineStatusData,
    *,
    workspace: Path,
    mode: StatusRenderMode,
    retry_on_label: str | None = None,
) -> list[str]:
    from litehive.observability.engine_monitoring import render_engine_monitoring_lines

    if mode == "full":
        lines = render_full_status_header_lines(workspace, status.config, status.state, status.runner)
    else:
        lines = [
            f"workspace: {workspace}",
            f"default_engine: {status.config.default_engine}",
            "mode: implementation",
            f"active_task_id: {status.active_task_id if status.active_task_id is not None else 'None'}",
            f"queued_tasks: {len(status.state.queue)}",
            f"pool_stop_reason: {status.state.pool_stop_reason if status.state.pool_stop_reason is not None else 'None'}",
        ]

    lines.extend(status.waiting_lines)
    if status.queue_head is not None:
        lines.append(f"queue_head: {status.queue_head}")

    if mode == "fast":
        lines.append(f"runner_status: {status.fast_runner_status}")
        if status.runner.pid is not None:
            lines.append(f"runner_pid: {status.runner.pid}")
        if status.runner.started_at:
            lines.append(f"runner_started_at: {status.runner.started_at}")
        if status.runner.heartbeat_at:
            lines.append(f"runner_heartbeat_at: {status.runner.heartbeat_at}")

    lines.extend(render_active_task_detail_lines(status.active_task, status.config.default_engine))
    lines.extend(render_engine_monitoring_lines(status.monitoring))

    if mode == "full":
        if retry_on_label is None:
            raise ValueError("retry_on_label is required when rendering full status output")
        lines.extend(render_runtime_policy_lines(status.config, retry_on_label))

    return lines


def _fast_runner_state_label(workspace: Path, runner: RunnerStatusState) -> str:
    if runner.status in {"running", "late"}:
        return "running"
    if not workspace_path(workspace, "runtime", ".runner.lock").exists():
        return "never_started"
    if runner.pid is None:
        return "stopped"
    return "dead"


def estimate_task_execution(root: Path, task: TaskRecord) -> ExecutionEstimate:
    """Return velocity and ETA estimate based on workspace report history."""
    durations = _collect_report_durations(root)
    if not durations:
        return ExecutionEstimate()

    avg_duration = sum(durations) / len(durations)
    velocity = 3600.0 / avg_duration if avg_duration > 0 else 0.0

    current_step = task.runtime.current_stage.stage or task.pipeline_status or _PIPELINE_STAGES[0]
    try:
        current_idx = _PIPELINE_STAGES.index(current_step)
    except ValueError:
        current_idx = 0
    remaining_stages = len(_PIPELINE_STAGES) - current_idx
    remaining_seconds = remaining_stages * avg_duration

    return ExecutionEstimate(
        stage_duration_seconds=avg_duration,
        remaining_seconds=remaining_seconds,
        velocity_stages_per_hour=velocity,
    )


def _collect_report_durations(root: Path) -> list[float]:
    """Scan all workspace report YAMLs and collect positive duration_seconds values."""
    tasks_dir = root / ".litehive" / "tasks"
    if not tasks_dir.exists():
        return []
    durations: list[float] = []
    for reports_dir in tasks_dir.glob("*/reports"):
        for report_path in sorted(reports_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
            except (yaml.YAMLError, OSError):
                continue
            dur = data.get("duration_seconds", 0)
            if isinstance(dur, (int, float)) and dur > 0:
                durations.append(float(dur))
    return durations


def _task_engine_label(task: TaskRecord, default_engine: str) -> str:
    return (
        task.runtime.active_subagent.engine
        if task.runtime.active_subagent is not None
        else task.runtime.last_subagent.engine
        if task.runtime.last_subagent is not None
        else default_engine
    )


def _task_stage_label(task: TaskRecord) -> str:
    return task.runtime.current_stage.stage or task.pipeline_status or "-"


def _task_last_verdict_label(task: TaskRecord) -> str:
    return task.runtime.last_stage.verdict or task.runtime.last_outcome.kind or "-"


def _task_last_summary_label(task: TaskRecord) -> str:
    return task.runtime.last_stage.summary or task.runtime.last_outcome.reason or task.flag_reason or "-"


def _latest_stage_failure_classification(root: Path | None, task: TaskRecord) -> str | None:
    if root is None:
        return None
    try:
        from litehive.tasks.reports import latest_stage_report

        report = latest_stage_report(root, task)
    except Exception:
        return None
    if report is None:
        return None
    return report.failure_classification


def render_task_summary(task: TaskRecord, *, active: bool, root: Path | None = None) -> list[str]:
    marker = "*" if active else " "
    retry_policy = task.retry_policy.max_retries
    retry_label = "default" if retry_policy is None else str(retry_policy)
    lines = [f"{marker} {task.id} [{task.status}/{task.pipeline_status}] retry_limit={retry_label} {task.title}"]
    if task.depends_on:
        lines.append(f"  depends_on={', '.join(task.depends_on)}")
    if task.model:
        lines.append(f"  engine=workspace-default model={task.model or 'default'}")
    _wt_path = task.runtime.git.worktree_path or task.git.worktree_path
    if task.status == "merge_failed" and _wt_path:
        lines.append(f"  unmerged_worktree={_wt_path}")
    lines.append(f"  auto_commit={task.git.auto_commit}")
    if task.git.commit_message:
        lines.append(f"  commit_message={task.git.commit_message}")
    if task.status == "flagged":
        lines.append(f"  flag_reason={task.flag_reason or 'unknown'}")

    runtime = task.runtime
    configured_limit = retry_policy if retry_policy is not None else "default"
    lines.append(f"  retry_policy=configured:{configured_limit} effective:{runtime.retry_limit}")
    if (
        runtime.execution_status != "idle"
        or runtime.current_stage.stage
        or runtime.current_stage.status != "idle"
        or runtime.last_stage.stage
    ):
        parts = [f"run={runtime.execution_status}"]
        parts.append(f"retries={runtime.retry_count}/{runtime.retry_limit}")
        if runtime.run_started_at:
            parts.append(f"started={runtime.run_started_at}")
        if runtime.current_stage.status != "idle":
            parts.append(f"stage_status={runtime.current_stage.status}")
        if runtime.current_stage.stage:
            stage_duration = _duration_label(runtime.current_stage.started_at, runtime.current_stage.duration_seconds)
            parts.append(f"stage={runtime.current_stage.stage}")
            parts.append(f"stage_duration={stage_duration}")
        elif runtime.last_stage.stage:
            parts.append(f"last_stage={runtime.last_stage.stage}")
            parts.append(f"last_stage_duration={_seconds_label(runtime.last_stage.duration_seconds)}")
        lines.append("  " + " ".join(parts))

    if runtime.active_subagent is not None:
        subagent_duration = _duration_label(runtime.active_subagent.started_at, 0)
        pid_label = _pid_label(runtime.active_subagent.pid)
        sandbox_label = _sandbox_label(
            runtime.active_subagent.sandboxed,
            runtime.active_subagent.sandbox_summary,
        )
        lines.append(
            "  "
            + (
                f"subagent={runtime.active_subagent.id} {runtime.active_subagent.role}/{runtime.active_subagent.engine} "
                f"{runtime.active_subagent.status} {pid_label} duration={subagent_duration} {sandbox_label}"
            )
        )
    elif runtime.last_subagent is not None:
        snippet = runtime.last_subagent.transcript_snippet or "-"
        pid_label = _pid_label(runtime.last_subagent.pid)
        sandbox_label = _sandbox_label(
            runtime.last_subagent.sandboxed,
            runtime.last_subagent.sandbox_summary,
        )
        lines.append(
            "  "
            + (
                f"last_subagent={runtime.last_subagent.id} {runtime.last_subagent.role}/{runtime.last_subagent.engine} "
                f"{runtime.last_subagent.status} {pid_label} {sandbox_label} snippet={snippet}"
            )
        )
        if runtime.last_subagent.interruption_reason:
            lines.append(f"  last_subagent_interruption_reason={runtime.last_subagent.interruption_reason}")

    if runtime.last_engine_switch is not None:
        lines.append(
            "  "
            + (
                f"engine_switch={runtime.last_engine_switch.stage} "
                f"{runtime.last_engine_switch.from_engine}->{runtime.last_engine_switch.to_engine} "
                f"reason={runtime.last_engine_switch.reason}"
            )
        )

    if runtime.interruption is not None:
        interruption = runtime.interruption
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

    if runtime.continuation_handoff is not None:
        handoff = runtime.continuation_handoff
        continuation_parts = [
            f"continuation={handoff.kind}",
            f"stage={handoff.stage}",
            f"reason={handoff.reason}",
        ]
        if handoff.from_engine or handoff.to_engine:
            continuation_parts.append(
                f"engine={handoff.from_engine or '-'}->{handoff.to_engine or handoff.from_engine or '-'}"
            )
        if handoff.subagent_id:
            continuation_parts.append(f"subagent={handoff.subagent_id}")
        if handoff.continuation is not None and handoff.continuation.resume_id:
            continuation_parts.append(f"resume_id={handoff.continuation.resume_id}")
        lines.append("  " + " ".join(continuation_parts))

    if runtime.last_stage.stage:
        summary = runtime.last_stage.summary or "-"
        lines.append(
            "  "
            + (
                f"last_report={runtime.last_stage.stage}/{runtime.last_stage.verdict} "
                f"duration={_seconds_label(runtime.last_stage.duration_seconds)} summary={summary}"
            )
        )
    report_classification = _latest_stage_failure_classification(root, task)
    if report_classification is not None:
        lines.append(f"  last_report_failure_classification={report_classification}")

    if runtime.last_outcome.kind is not None:
        stage = runtime.last_outcome.stage or "-"
        reason_code = runtime.last_outcome.reason_code or "-"
        reason = runtime.last_outcome.reason or "-"
        recorded_at = runtime.last_outcome.recorded_at or "-"
        follow_up_task_id = runtime.last_outcome.follow_up_task_id or "-"
        lines.append(
            "  "
            + (
                f"outcome={runtime.last_outcome.kind} stage={stage} "
                f"reason_code={reason_code} recorded_at={recorded_at} "
                f"follow_up_task={follow_up_task_id} "
                f"retry_state={runtime.last_outcome.retry_count}/{runtime.last_outcome.retry_limit} "
                f"reason={reason}"
            )
        )
        if runtime.last_outcome.failure_classification is not None:
            phase = runtime.last_outcome.failure_diagnostics.get("phase", "-")
            lines.append(
                "  " + (f"failure_classification={runtime.last_outcome.failure_classification} failure_phase={phase}")
            )

    if task.status == "merge_failed":
        wt_path = task.runtime.git.worktree_path or task.git.worktree_path
        if wt_path:
            lines.append(f"  unmerged_worktree={wt_path}")
    if task.git.commit_sha:
        lines.append(f"  commit={task.git.commit_sha}")

    if root is not None:
        estimate = estimate_task_execution(root, task)
        if estimate.stage_duration_seconds > 0:
            lines.append(
                f"  stage_estimate={_seconds_label(int(estimate.stage_duration_seconds))} "
                f"velocity={estimate.velocity_stages_per_hour:.1f}stages/h "
                f"eta={_seconds_label(int(estimate.remaining_seconds))}"
            )

    return lines


def _duration_label(started_at: str | None, fallback_seconds: int) -> str:
    if started_at is None:
        return _seconds_label(fallback_seconds)
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return _seconds_label(fallback_seconds)
    seconds = max(0, int((datetime.now(UTC) - started).total_seconds()))
    return _seconds_label(seconds)


def _seconds_label(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{remaining_seconds:02d}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h{remaining_minutes:02d}m"


def _pid_label(pid: int | None) -> str:
    return f"pid={pid}" if pid is not None else "pid=-"


def _sandbox_label(sandboxed: bool, sandbox_summary: str) -> str:
    return f"sandbox={sandbox_summary or 'enabled'}" if sandboxed else "sandbox=host"


# ---------------------------------------------------------------------------
# Dashboard-style status output
# ---------------------------------------------------------------------------


def render_active_task_section(task: TaskRecord | None, default_engine: str) -> list[str]:
    """Render the Active Task dashboard section."""
    lines: list[str] = ["=== Active Task ==="]
    if task is None:
        lines.append("  (none)")
        return lines

    engine = _task_engine_label(task, default_engine)
    stage = _task_stage_label(task)

    # Task elapsed duration (since run started)
    task_duration = _duration_label(task.runtime.run_started_at, 0)

    # Stage elapsed duration
    stage_duration = _duration_label(
        task.runtime.current_stage.started_at,
        task.runtime.current_stage.duration_seconds,
    )

    lines.append(f"  {task.id} {stage} with {engine}, running for {task_duration}")
    lines.append(f"  stage: {stage} elapsed {stage_duration}")
    lines.append(f"  title: {task.title}")

    return lines


def render_active_task_detail_lines(task: TaskRecord | None, default_engine: str) -> list[str]:
    """Render the flat active-task detail lines used by CLI status outputs."""
    if task is None:
        return []

    engine = _task_engine_label(task, default_engine)
    stage = _task_stage_label(task)
    return [
        f"active_task_title: {task.title}",
        f"active_task_status: {task.status}/{task.pipeline_status}",
        f"active_stage: {stage}",
        f"active_engine: {engine}",
    ]


def render_runner_status_line(runner: RunnerStatusState, state: WorkspaceState | None = None) -> str:
    del state
    base_status = (
        f"runner_status: {runner.status} pid={runner.pid or '-'} "
        f"started_at={runner.started_at or '-'} "
        f"heartbeat_at={runner.heartbeat_at or '-'}"
    )
    return f"{base_status} active_task_id={runner.active_task_id or '-'}"


def render_full_status_header_lines(
    workspace: Path,
    config: LitehiveConfig,
    state: WorkspaceState,
    runner: RunnerStatusState,
) -> list[str]:
    active_task_id = runner.active_task_id or state.active_task_id
    lines = [
        f"workspace: {workspace}",
        "status_read_mode: full",
        f"default_engine: {config.default_engine}",
    ]
    freezes = active_engine_freezes(config)
    if freezes:
        for engine_name, until_dt in sorted(freezes.items()):
            local_until = until_dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")
            lines.append(f"engine_frozen: {engine_name} until {local_until}")

    lines.extend(
        [
            f"litehive_source_path: {config.litehive_source_path or '-'}",
            f"active_task_id: {active_task_id}",
            render_runner_status_line(runner, state),
            f"queued_tasks: {len(state.queue)}",
            f"pool_stop_reason: {state.pool_stop_reason}",
        ]
    )
    return lines


def render_runtime_policy_lines(config: LitehiveConfig, retry_on_label: str) -> list[str]:
    return [
        f"default_retry_limit: {config.default_retry_limit}",
        f"retry_on: {retry_on_label}",
        f"pool_stop_on_failure: {config.pool_stop_on_failure}",
        f"pool_max_tasks: {config.pool_max_tasks}",
        f"pool_stop_on_dirty_git: {config.pool_stop_on_dirty_git}",
        f"pool_stop_on_attention: {config.pool_stop_on_attention}",
        f"process_profile: {config.process_profile}",
    ]


def render_active_tasks_section(
    tasks: list[TaskRecord],
    default_engine: str,
) -> list[str]:
    """Render the Active Tasks dashboard section."""
    lines: list[str] = ["=== Active Tasks ==="]
    if not tasks:
        lines.append("  (none)")
        return lines

    lines.append(f"  {len(tasks)} active task(s)")
    for task in tasks:
        engine = _task_engine_label(task, default_engine)
        stage = _task_stage_label(task)
        task_duration = _duration_label(task.runtime.run_started_at, 0)
        worktree = task.runtime.git.worktree_path or task.git.worktree_path or "-"

        lines.append(f"  {task.id} {stage} with {engine}, running for {task_duration}")
        lines.append(f"    title: {task.title}")
        lines.append(f"    worktree: {worktree}")

    return lines


def find_last_completed_task(tasks: list[TaskRecord]) -> TaskRecord | None:
    """Return the most recently completed (done) task by updated_at."""
    done_tasks = [t for t in tasks if t.status == "done"]
    if not done_tasks:
        return None
    return max(done_tasks, key=lambda t: t.updated_at or "")


def render_last_completed_section(task: TaskRecord | None) -> list[str]:
    """Render the Last Completed dashboard section."""
    lines: list[str] = ["=== Last Completed ==="]
    if task is None:
        lines.append("  (none)")
        return lines

    outcome = task.runtime.last_outcome
    verdict = outcome.kind or _task_last_verdict_label(task)
    when = outcome.recorded_at or task.updated_at or "-"
    lines.append(f"  {task.id} {task.title} verdict={verdict} at {when}")
    return lines


def render_health_active_task_lines(task: TaskRecord | None) -> list[str]:
    lines = ["=== Active Task ==="]
    if task is None:
        lines.append("active_task: none")
        return lines
    lines.append(
        f"active_task: {task.id} [{task.status}/{task.pipeline_status}] "
        f"stage={_task_stage_label(task)} title={task.title}"
    )
    return lines


def render_health_flagged_task_lines(flagged_tasks: list[TaskRecord]) -> list[str]:
    lines = ["=== Flagged Tasks ===", f"flagged_count: {len(flagged_tasks)}"]
    if not flagged_tasks:
        lines.append("flagged: none")
        return lines
    for task in flagged_tasks:
        lines.append(
            f"flagged: {task.id} stage={_task_stage_label(task)} "
            f"reason={task.flag_reason or 'unknown'} "
            f"last_verdict={_task_last_verdict_label(task)} "
            f"summary={_task_last_summary_label(task)}"
        )
    return lines


def render_health_worktree_lines(worktrees: list[Any]) -> list[str]:
    lines = ["=== Worktrees ===", f"worktree_count: {len(worktrees)}"]
    if not worktrees:
        lines.append("worktree: none")
        return lines
    for item in worktrees:
        lines.append(
            f"worktree: {item.task_id} status={item.status} changes={item.change_count} "
            f"active={'yes' if item.active else 'no'} path={item.worktree_rel}"
        )
    return lines


def render_health_worktree_finding_lines(report: Any) -> list[str]:
    lines = ["=== Worktree Findings ==="]
    if report.is_clean:
        lines.append("worktree_findings: clean")
        return lines
    for finding in report.findings:
        details = [f"location={finding.location_kind}", f"ownership={finding.ownership}"]
        if finding.task_id:
            details.append(f"task_id={finding.task_id}")
        if finding.worktree_path:
            details.append(f"path={finding.worktree_path}")
        details.append("dirty_paths=" + (",".join(finding.dirty_paths) if finding.dirty_paths else "-"))
        lines.append("finding: " + " ".join(details))
    return lines


def render_health_quota_lines(quota_health: list[Any]) -> list[str]:
    lines = ["=== Engine Quotas ==="]
    for quota in quota_health:
        lines.append(f"quota: {quota.engine} status={quota.status} summary={quota.summary}")
    return lines


def render_health_daemon_lines(daemon_status: str, daemon_pid: str) -> list[str]:
    return [
        "=== Daemon ===",
        f"daemon_status: {daemon_status}",
        f"daemon_pid: {daemon_pid}",
    ]


def render_health_recent_completion_lines(completed: list[TaskRecord]) -> list[str]:
    lines = ["=== Recent Completions ==="]
    if not completed:
        lines.append("completed: none")
        return lines
    for task in completed:
        lines.append(
            f"completed: {task.id} title={task.title} when={task.updated_at or '-'} "
            f"summary={_task_last_summary_label(task)}"
        )
    return lines


def render_queue_section(queue: list[str], tasks: list[TaskRecord]) -> list[str]:
    """Render the Queue dashboard section."""
    lines: list[str] = ["=== Queue ==="]
    count = len(queue)
    if count == 0:
        lines.append("  (empty)")
        return lines

    task_by_id = {t.id: t for t in tasks}
    head_id = queue[0]
    head_task = task_by_id.get(head_id)
    head_title = head_task.title if head_task else head_id
    lines.append(f"  {count} queued, next: {head_id} {head_title}")
    return lines


def render_engine_health_section(monitoring: WorkspaceEngineMonitoring) -> list[str]:
    """Render the Engine Health dashboard section."""
    lines: list[str] = ["=== Engine Health ==="]
    if not monitoring.engines:
        lines.append("  (no engine data)")
        return lines

    for engine_name in sorted(monitoring.engines):
        record = monitoring.engines[engine_name]
        if record.last_limit_kind in ("quota", "rate", "budget"):
            reset_label = ""
            if record.usage is not None and record.usage.reset_at:
                reset_label = f" resets {record.usage.reset_at}"
            lines.append(f"  {engine_name}: {record.last_limit_kind}{reset_label}")
        else:
            usage_label = ""
            if record.usage is not None and record.usage.used is not None:
                if record.usage.limit is not None:
                    usage_label = f" ({record.usage.used}/{record.usage.limit} {record.usage.unit or 'used'})"
                else:
                    usage_label = f" ({record.usage.used} {record.usage.unit or 'used'})"
            lines.append(f"  {engine_name}: ok{usage_label}")
    return lines


def collect_recent_activity(root: Path, *, limit: int = 5) -> list[dict[str, Any]]:
    """Scan events.jsonl across all task dirs, return most recent events."""
    tasks_dir = root / ".litehive" / "tasks"
    if not tasks_dir.exists():
        return []
    all_events: list[dict[str, Any]] = []
    for events_path in tasks_dir.glob("*/events.jsonl"):
        try:
            text = events_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                all_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    all_events.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return all_events[:limit]


_EVENT_LABELS: dict[str, str] = {
    "stage_completed": "stage completed",
    "stage_started": "stage started",
    "subagent_started": "subagent started",
    "subagent_completed": "subagent completed",
    "subagent_finished": "finished",
    "subagent_pid": "running",
    "subagent_progress": "progress",
    "engine_switch": "engine switched",
    "engine_fallback": "engine fallback",
    "task_queued": "task queued",
    "task_completed": "task completed",
    "task_flagged": "task flagged",
    "task_interrupted": "task interrupted",
    "execution_error": "error",
    "retry": "retry",
}


def render_recent_activity_section(events: list[dict[str, Any]]) -> list[str]:
    """Render the Recent Activity dashboard section."""
    lines: list[str] = ["=== Recent Activity ==="]
    if not events:
        lines.append("  (no recent activity)")
        return lines
    for event in events:
        kind = event.get("kind", "unknown")
        label = _EVENT_LABELS.get(kind, kind)
        ts = event.get("ts", "-")
        task_id = event.get("task_id", "-")
        data = event.get("data", {})
        detail_parts = [f"{task_id} {label}"]
        if kind in ("stage_completed", "stage_started") and data.get("stage"):
            detail_parts.append(data["stage"])
        if kind == "engine_switch" and data.get("to_engine"):
            detail_parts.append(f"-> {data['to_engine']}")
        if "role" in data:
            detail_parts.append(data["role"])
        if "engine" in data:
            detail_parts.append(data["engine"])
        if "exit_code" in data:
            detail_parts.append(f"exit={data['exit_code']}")
        if data.get("verdict"):
            detail_parts.append(f"verdict={data['verdict']}")
        lines.append(f"  [{ts}] {' '.join(detail_parts)}")
    return lines
