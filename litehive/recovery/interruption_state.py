"""Task-level interruption bookkeeping: stamping a task as ``INTERRUPTED`` and rendering the operator-visible journal line."""

from pathlib import Path

from litehive.domain.common import TaskStatus, utcnow
from litehive.domain.runtime import RuntimeInterruptionState
from litehive.domain.task import TaskRecord
from litehive.recovery.interrupted_subagent import mark_interrupted_subagent
from litehive.tasks.runtime import (
    apply_task_outcome,
    duration_seconds,
)


def prepare_interrupted_task(
    root: Path,
    task: TaskRecord,
    stage: str,
    summary: str,
    reason: str | None = None,
) -> None:
    """Stamp a task as ``INTERRUPTED`` at ``stage`` so the next dequeue resumes from a known recovery point; called by the queue's stop/recovery flows when a runner crash or stale-runner detection requires the in-flight stage to be requeued."""
    now = utcnow()
    interruption_reason = reason or summary
    timestamps = _interruption_timestamps(task, now)
    task.status = TaskStatus.INTERRUPTED
    task.pipeline_status = stage
    task.runtime.pipeline.execution_status = "interrupted"
    task.runtime.pipeline.run_started_at = None
    task.runtime.pipeline.updated_at = now
    _set_interruption_metadata(
        task,
        root=root,
        stage=stage,
        summary=summary,
        reason=interruption_reason,
        now=now,
        started_at=timestamps["started_at"],
        run_started_at=timestamps["run_started_at"],
        stage_started_at=timestamps["stage_started_at"],
        interrupted_at=timestamps["interrupted_at"],
    )


def interruption_journal_message(task: TaskRecord) -> str:
    """Render the human-readable journal line that the queue/stop flows persist when an interruption is recorded; pulls subagent details (pid, role, snippet) out of runtime state so the operator-visible journal explains *why* the task is paused."""
    interruption = task.runtime.execution.interruption
    if interruption is None:
        return f"Interrupted run recorded. Resume from `{task.pipeline_status}`."
    parts = [
        (
            f"Interrupted {interruption.source} execution while "
            f"`{interruption.stage or task.pipeline_status}` was running."
        ),
        f"Reason: {interruption.reason or interruption.summary or 'unknown'}.",
    ]
    if interruption.subagent is not None:
        subagent = interruption.subagent
        if subagent.pid is not None:
            pid = subagent.pid
        else:
            pid = "-"
        parts.append(
            f"Subagent `{subagent.id}` ({subagent.role}/{subagent.engine}, pid={pid}, "
            f"path `{subagent.path}`) stopped with status `{subagent.status}`."
        )
        if subagent.execution_trace_snippet:
            parts.append(f"Last snippet: {subagent.execution_trace_snippet}.")
    parts.append(f"Resume from `{interruption.resume_stage or task.pipeline_status}`.")
    return " ".join(parts)


def stale_interruption_reason(task: TaskRecord, stage: str, stale_pid: bool = False) -> str:
    """Build the ``reason`` string the stale-runner repair attaches to the task; embeds subagent role/engine and (when known) "pid no longer alive" so the recovery report distinguishes "runner died" from "subagent died" failures."""
    active = task.runtime.execution.active_subagent
    if active is None:
        return f"Stale runner detected while `{stage}` was still marked running."
    if stale_pid and active.pid:
        pid_detail = f", pid {active.pid} no longer alive"
    else:
        pid_detail = ""
    return (
        f"Stale runner detected while subagent `{active.id}` "
        f"({active.role}/{active.engine}{pid_detail}) was still marked running in `{stage}`."
    )


def _interruption_timestamps(task: TaskRecord, now: str) -> dict[str, str | None]:
    started_at = task.runtime.pipeline.current_stage.started_at or task.runtime.pipeline.run_started_at
    if task.runtime.execution.active_subagent is not None:
        interrupted_at = task.runtime.execution.active_subagent.updated_at
    else:
        interrupted_at = task.runtime.pipeline.current_stage.updated_at or started_at or now
    return {
        "run_started_at": task.runtime.pipeline.run_started_at,
        "stage_started_at": task.runtime.pipeline.current_stage.started_at,
        "started_at": started_at,
        "interrupted_at": interrupted_at,
    }


def _set_interruption_metadata(
    task: TaskRecord,
    root: Path,
    stage: str,
    summary: str,
    reason: str,
    now: str,
    started_at: str | None,
    run_started_at: str | None,
    stage_started_at: str | None,
    interrupted_at: str | None,
) -> None:
    """One-shot mutation that wires together the interruption record, the apply-outcome bookkeeping, and the current-stage snapshot; centralised here so the three layers (task outcome, interruption state, current stage) stay consistent in time."""
    interrupted_subagent = mark_interrupted_subagent(root, task, reason=reason, stage=stage)
    apply_task_outcome(
        task,
        kind="interrupted",
        stage=stage,
        reason_code="execution_interrupted",
        reason=summary,
        retry_count=task.runtime.pipeline.retry_count,
        retry_limit=task.runtime.pipeline.retry_limit,
    )
    if interrupted_subagent is not None:
        interruption_source = "subagent"
    else:
        interruption_source = "runner"
    task.runtime.execution.interruption = RuntimeInterruptionState(
        source=interruption_source,
        stage=stage,
        pipeline_status=stage,
        resume_stage=stage,
        reason=reason,
        summary=summary,
        interrupted_at=interrupted_at,
        detected_at=now,
        run_started_at=run_started_at,
        stage_started_at=stage_started_at,
        subagent=interrupted_subagent,
    )
    task.runtime.pipeline.current_stage = task.runtime.pipeline.current_stage.model_copy(
        update={
            "stage": stage,
            "status": "interrupted",
            "started_at": started_at,
            "updated_at": now,
            "duration_seconds": duration_seconds(started_at, now),
        }
    )
