"""Shared task observability formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from litehive.models import TaskRecord


def render_task_summary(task: TaskRecord, *, active: bool) -> list[str]:
    marker = "*" if active else " "
    lines = [f"{marker} {task.id} [{task.status}/{task.pipeline_status}] {task.mode} {task.title}"]

    runtime = task.runtime
    if runtime.execution_status != "idle" or runtime.current_stage.step or runtime.last_stage.step:
        parts = [f"run={runtime.execution_status}"]
        if runtime.run_started_at:
            parts.append(f"started={runtime.run_started_at}")
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
        lines.append(
            "  "
            + (
                f"subagent={runtime.active_subagent.id} {runtime.active_subagent.role}/{runtime.active_subagent.engine} "
                f"{runtime.active_subagent.status} duration={subagent_duration}"
            )
        )
    elif runtime.last_subagent is not None:
        snippet = runtime.last_subagent.transcript_snippet or "-"
        lines.append(
            "  "
            + (
                f"last_subagent={runtime.last_subagent.id} {runtime.last_subagent.role}/{runtime.last_subagent.engine} "
                f"{runtime.last_subagent.status} snippet={snippet}"
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

    if runtime.last_stage.step:
        summary = runtime.last_stage.summary or "-"
        lines.append(
            "  "
            + (
                f"last_report={runtime.last_stage.step}/{runtime.last_stage.verdict} "
                f"duration={_seconds_label(runtime.last_stage.duration_seconds)} summary={summary}"
            )
        )

    if task.git.commit_sha:
        lines.append(f"  commit={task.git.commit_sha}")

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
