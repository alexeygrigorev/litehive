"""Shared task observability formatting helpers."""

import json
from pathlib import Path
from datetime import UTC, datetime
from typing import Any

import yaml

from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.reports import ExecutionEstimate
from litehive.domain.task import TaskRecord

# Ordered pipeline stages for remaining-time estimation.
_PIPELINE_STAGES = ["grooming", "implementing", "testing", "accepting", "commit_to_git"]


def estimate_task_execution(root: Path, task: TaskRecord) -> ExecutionEstimate:
    """Return velocity and ETA estimate based on workspace report history."""
    durations = _collect_report_durations(root)
    if not durations:
        return ExecutionEstimate()

    avg_duration = sum(durations) / len(durations)
    velocity = 3600.0 / avg_duration if avg_duration > 0 else 0.0

    current_step = (
        task.runtime.current_stage.step
        or task.pipeline_status
        or _PIPELINE_STAGES[0]
    )
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

    runtime = task.runtime
    configured_limit = retry_policy if retry_policy is not None else "default"
    lines.append(
        f"  retry_policy=configured:{configured_limit} effective:{runtime.retry_limit}"
    )
    if (
        runtime.execution_status != "idle"
        or runtime.current_stage.step
        or runtime.current_stage.status != "idle"
        or runtime.last_stage.step
    ):
        parts = [f"run={runtime.execution_status}"]
        parts.append(f"retries={runtime.retry_count}/{runtime.retry_limit}")
        if runtime.run_started_at:
            parts.append(f"started={runtime.run_started_at}")
        if runtime.current_stage.status != "idle":
            parts.append(f"stage_status={runtime.current_stage.status}")
        if runtime.current_stage.step:
            stage_duration = _duration_label(runtime.current_stage.started_at, runtime.current_stage.duration_seconds)
            parts.append(f"stage={runtime.current_stage.step}")
            parts.append(f"stage_duration={stage_duration}")
        elif runtime.last_stage.step:
            parts.append(f"last_stage={runtime.last_stage.step}")
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
        if runtime.last_subagent.resource_limit_event is not None:
            event = runtime.last_subagent.resource_limit_event
            limit_parts: list[str] = []
            if event.memory_mb is not None:
                limit_parts.append(f"memory_mb={event.memory_mb}")
            if event.cpu_count is not None:
                limit_parts.append(f"cpu_count={event.cpu_count:g}")
            if event.process_limit is not None:
                limit_parts.append(f"process_limit={event.process_limit}")
            signal = event.observed_signal or "-"
            lines.append(
                "  "
                + (
                    f"resource_limit={event.resource} signal={signal} exit_code={event.exit_code or '-'} "
                    f"limits={','.join(limit_parts) or '-'} reason={event.reason}"
                )
            )

    if runtime.last_engine_switch is not None:
        lines.append(
            "  "
            + (
                f"engine_switch={runtime.last_engine_switch.step} "
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
            f"step={handoff.step}",
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

    if runtime.last_stage.step:
        summary = runtime.last_stage.summary or "-"
        lines.append(
            "  "
            + (
                f"last_report={runtime.last_stage.step}/{runtime.last_stage.verdict} "
                f"duration={_seconds_label(runtime.last_stage.duration_seconds)} summary={summary}"
            )
        )

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
                "  "
                + (
                    f"failure_classification={runtime.last_outcome.failure_classification} "
                    f"failure_phase={phase}"
                )
            )

    if task.status == "merge_failed":
        wt_path = task.runtime.git.worktree_path or task.git.worktree_path
        if wt_path:
            lines.append(f"  unmerged_worktree={wt_path}")
    if task.git.commit_sha:
        lines.append(f"  commit={task.git.commit_sha}")
    if task.git.checkpoint_base_sha:
        lines.append(f"  checkpoint_base={task.git.checkpoint_base_sha}")
    if task.git.rolled_back_checkpoint_attempt is not None:
        lines.append(f"  rolled_back_attempt={task.git.rolled_back_checkpoint_attempt}")

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

    engine = (
        task.runtime.active_subagent.engine
        if task.runtime.active_subagent is not None
        else task.runtime.last_subagent.engine
        if task.runtime.last_subagent is not None
        else default_engine
    )
    stage = task.runtime.current_stage.step or task.pipeline_status or "-"

    # Task elapsed duration (since run started)
    task_duration = _duration_label(task.runtime.run_started_at, 0)

    # Stage elapsed duration
    stage_duration = _duration_label(
        task.runtime.current_stage.started_at,
        task.runtime.current_stage.duration_seconds,
    )

    lines.append(
        f"  {task.id} {stage} with {engine}, running for {task_duration}"
    )
    lines.append(f"  stage: {stage} elapsed {stage_duration}")
    lines.append(f"  title: {task.title}")

    return lines


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
        engine = (
            task.runtime.active_subagent.engine
            if task.runtime.active_subagent is not None
            else task.runtime.last_subagent.engine
            if task.runtime.last_subagent is not None
            else default_engine
        )
        stage = task.runtime.current_stage.step or task.pipeline_status or "-"
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
    verdict = outcome.kind or task.runtime.last_stage.verdict or "-"
    when = outcome.recorded_at or task.updated_at or "-"
    lines.append(f"  {task.id} {task.title} verdict={verdict} at {when}")
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
        if kind in ("stage_completed", "stage_started") and data.get("step"):
            detail_parts.append(data["step"])
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
