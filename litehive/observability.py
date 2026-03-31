"""Shared task observability formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from litehive.models import TaskRecord


def render_task_summary(task: TaskRecord, *, active: bool) -> list[str]:
    marker = "*" if active else " "
    retry_policy = task.retry_policy.max_retries
    retry_label = "default" if retry_policy is None else str(retry_policy)
    lines = [f"{marker} {task.id} [{task.status}/{task.pipeline_status}] {task.mode} retry_limit={retry_label} {task.title}"]
    if task.depends_on:
        lines.append(f"  depends_on={', '.join(task.depends_on)}")
    if task.human_checkpoints:
        lines.append(f"  human_checkpoints={', '.join(task.human_checkpoints)}")
    lines.append(f"  auto_commit={task.git.auto_commit}")
    if task.git.commit_message:
        lines.append(f"  commit_message={task.git.commit_message}")

    runtime = task.runtime
    configured_limit = retry_policy if retry_policy is not None else "default"
    lines.append(
        f"  retry_policy=configured:{configured_limit} effective:{runtime.retry_limit} source={runtime.retry_source}"
    )
    if runtime.execution_status != "idle" or runtime.current_stage.step or runtime.last_stage.step:
        parts = [f"run={runtime.execution_status}"]
        parts.append(f"retries={runtime.retry_count}/{runtime.retry_limit}")
        parts.append(f"retry_source={runtime.retry_source}")
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

    if runtime.last_outcome.kind is not None:
        stage = runtime.last_outcome.stage or "-"
        reason_code = runtime.last_outcome.reason_code or "-"
        reason = runtime.last_outcome.reason or "-"
        recorded_at = runtime.last_outcome.recorded_at or "-"
        lines.append(
            "  "
            + (
                f"outcome={runtime.last_outcome.kind} stage={stage} "
                f"reason_code={reason_code} recorded_at={recorded_at} "
                f"retry_state={runtime.last_outcome.retry_count}/{runtime.last_outcome.retry_limit} "
                f"retry_source={runtime.last_outcome.retry_source} reason={reason}"
            )
        )

    if task.git.commit_sha:
        lines.append(f"  commit={task.git.commit_sha}")
    if task.git.checkpoint_base_sha:
        lines.append(f"  checkpoint_base={task.git.checkpoint_base_sha}")
    if task.git.rolled_back_checkpoint_attempt is not None:
        lines.append(f"  rolled_back_attempt={task.git.rolled_back_checkpoint_attempt}")

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
