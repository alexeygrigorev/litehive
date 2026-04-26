"""Runtime state tracking: runs, stages, subagents, engine switches."""

from pathlib import Path

from litehive.domain.common import utcnow
from litehive.domain.reports import StageReport
from litehive.domain.runtime import (
    RuntimeContinuationHandoff,
    RuntimeEngineContinuation,
    RuntimeEngineSwitch,
    RuntimeStageState,
    SubagentRef,
    RuntimeSubagentState,
    TaskOutcomeState,
)
from litehive.domain.task import TaskRecord

from litehive.state.records import write_task_runtime, save_task_runtime
from litehive.state.locking import workspace_lock, workspace_mutation_guard
from litehive.state.persist import load_state
from litehive.state.persist import persist_task_and_state


def idle_stage_state(*, updated_at: str, stage: str | None = None) -> RuntimeStageState:
    return RuntimeStageState(stage=stage, updated_at=updated_at)


def _running_stage_state(stage: str, *, started_at: str) -> RuntimeStageState:
    return RuntimeStageState(
        stage=stage,
        status="running",
        started_at=started_at,
        updated_at=started_at,
    )


def _completed_stage_state(
    report: StageReport,
    *,
    started_at: str | None,
    completed_at: str,
) -> RuntimeStageState:
    return RuntimeStageState(
        stage=report.pipeline_state,
        status="completed" if report.verdict == "pass" else report.verdict,
        started_at=started_at,
        completed_at=completed_at,
        updated_at=completed_at,
        duration_seconds=duration_seconds(started_at, completed_at),
        verdict=report.verdict,
        summary=report.summary,
    )


def _runtime_subagent_state(
    ref: SubagentRef,
    *,
    started_at: str,
    updated_at: str,
    pid: int | None = None,
    completed_at: str | None = None,
    exit_code: int | None = None,
    transcript_snippet: str = "",
    interruption_reason: str = "",
    continuation: RuntimeEngineContinuation | None = None,
) -> RuntimeSubagentState:
    return RuntimeSubagentState(
        id=ref.id,
        role=ref.role,
        engine=ref.engine,
        status=ref.status,
        path=ref.path,
        pid=pid,
        sandboxed=ref.sandboxed,
        sandbox_summary=ref.sandbox_summary,
        started_at=started_at,
        updated_at=updated_at,
        completed_at=completed_at,
        exit_code=exit_code,
        transcript_snippet=transcript_snippet,
        interruption_reason=interruption_reason,
        continuation=continuation,
    )


def clear_task_run_activity(
    task: TaskRecord,
    *,
    execution_status: str,
    updated_at: str | None = None,
    clear_interruption: bool = False,
) -> str:
    now = updated_at or utcnow()
    task.runtime.pipeline.execution_status = execution_status
    task.runtime.pipeline.run_started_at = None
    task.runtime.pipeline.updated_at = now
    task.runtime.execution.active_subagent = None
    if clear_interruption:
        task.runtime.execution.interruption = None
    return now


def mark_task_run_started(root: Path, task: TaskRecord) -> None:
    now = clear_task_run_activity(task, execution_status="running", clear_interruption=True)
    task.runtime.pipeline.run_started_at = now
    task.runtime.pipeline.retry_count = 0
    task.runtime.pipeline.retry_limit = task.runtime.pipeline.retry_limit
    task.runtime.pipeline.last_outcome = TaskOutcomeState()
    task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now)
    save_task_runtime(root, task)


def mark_task_run_finished(root: Path, task: TaskRecord, final_status: str) -> None:
    clear_task_run_activity(task, execution_status=final_status)
    save_task_runtime(root, task)


def apply_flag_count_auto_defer(task: TaskRecord) -> None:
    """Increment flag_count and auto-defer if the threshold is reached."""
    if task.status != "flagged":
        return
    task.flag_count += 1
    if task.flag_count >= 3:
        task.flag_reason = "flagged 3 times - needs human review"


def finish_task_run_transition(root: Path, task: TaskRecord, final_status: str) -> TaskRecord:
    with workspace_mutation_guard(root), workspace_lock(root):
        apply_flag_count_auto_defer(task)
        clear_task_run_activity(task, execution_status=final_status)
        state = load_state(root)
        state_changed = False
        if state.active_task_id == task.id:
            state.active_task_id = None
            state_changed = True
        queued_without_task = [item for item in state.queue if item != task.id]
        if queued_without_task != state.queue:
            state.queue = queued_without_task
            state_changed = True
        if (
            final_status in {"paused", "queued", "interrupted"}
            and task.status == "queued"
            and task.pipeline_status != "done"
        ):
            state.queue.insert(0, task.id)
            state_changed = True
        if final_status == "done" and task.status == "done" and task.pipeline_status == "done" and not state_changed:
            write_task_runtime(root, task)
            return task
        persist_task_and_state(root, task=task, state=state)
        return task


def set_task_retry_state(
    root: Path,
    task: TaskRecord,
    *,
    retry_count: int,
    retry_limit: int,
) -> None:
    _apply_task_retry_state(
        task,
        retry_count=retry_count,
        retry_limit=retry_limit,
    )
    save_task_runtime(root, task)


def clear_task_outcome(root: Path, task: TaskRecord) -> None:
    _clear_task_outcome(task)
    save_task_runtime(root, task)


def set_task_continuation_handoff(
    root: Path,
    task: TaskRecord,
    handoff: RuntimeContinuationHandoff | None,
) -> None:
    task.runtime.pipeline.updated_at = utcnow()
    task.runtime.execution.continuation_handoff = handoff
    save_task_runtime(root, task)


def clear_task_continuation_handoff(root: Path, task: TaskRecord) -> None:
    if task.runtime.execution.continuation_handoff is None:
        return
    set_task_continuation_handoff(root, task, None)


def _apply_task_retry_state(
    task: TaskRecord,
    *,
    retry_count: int,
    retry_limit: int,
) -> None:
    task.runtime.pipeline.updated_at = utcnow()
    task.runtime.pipeline.retry_count = retry_count
    task.runtime.pipeline.retry_limit = retry_limit


def _clear_task_outcome(task: TaskRecord) -> None:
    task.runtime.pipeline.updated_at = utcnow()
    task.runtime.pipeline.last_outcome = TaskOutcomeState()


def mark_task_outcome(
    root: Path,
    task: TaskRecord,
    *,
    kind: str,
    stage: str,
    reason_code: str,
    reason: str,
    retry_count: int,
    retry_limit: int,
    follow_up_task_id: str | None = None,
    failure_classification: str | None = None,
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] | None = None,
) -> None:
    apply_task_outcome(
        task,
        kind=kind,
        stage=stage,
        reason_code=reason_code,
        reason=reason,
        retry_count=retry_count,
        retry_limit=retry_limit,
        follow_up_task_id=follow_up_task_id,
        failure_classification=failure_classification,
        failure_diagnostics=failure_diagnostics,
    )
    save_task_runtime(root, task)


def apply_task_outcome(
    task: TaskRecord,
    *,
    kind: str,
    stage: str,
    reason_code: str,
    reason: str,
    retry_count: int,
    retry_limit: int,
    follow_up_task_id: str | None = None,
    failure_classification: str | None = None,
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] | None = None,
) -> None:
    now = utcnow()
    task.runtime.pipeline.updated_at = now
    task.runtime.pipeline.last_outcome = TaskOutcomeState(
        kind=kind,
        stage=stage,
        reason_code=reason_code,
        reason=reason,
        failure_classification=failure_classification,
        failure_diagnostics=dict(failure_diagnostics or {}),
        follow_up_task_id=follow_up_task_id,
        retry_count=retry_count,
        retry_limit=retry_limit,
        recorded_at=now,
    )


def mark_stage_started(root: Path, task: TaskRecord, stage: str) -> None:
    now = utcnow()
    task.runtime.pipeline.updated_at = now
    task.runtime.pipeline.current_stage = _running_stage_state(stage, started_at=now)
    save_task_runtime(root, task)


def mark_stage_finished(root: Path, task: TaskRecord, report: StageReport) -> None:
    apply_stage_finished(task, report)
    save_task_runtime(root, task)


def apply_stage_finished(task: TaskRecord, report: StageReport) -> None:
    now = utcnow()
    started_at = task.runtime.pipeline.current_stage.started_at
    task.runtime.pipeline.updated_at = now
    task.runtime.pipeline.last_stage = _completed_stage_state(report, started_at=started_at, completed_at=now)
    task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now)
    if (
        task.runtime.execution.continuation_handoff is not None
        and task.runtime.execution.continuation_handoff.stage == report.pipeline_state
    ):
        task.runtime.execution.continuation_handoff = None


def mark_subagent_started(root: Path, task: TaskRecord, ref: SubagentRef) -> None:
    now = utcnow()
    task.runtime.pipeline.updated_at = now
    task.runtime.execution.active_subagent = _runtime_subagent_state(ref, started_at=now, updated_at=now)
    save_task_runtime(root, task)


def mark_subagent_pid(root: Path, task: TaskRecord, pid: int | None) -> None:
    if (
        pid is None
        or task.runtime.execution.active_subagent is None
        or task.runtime.execution.active_subagent.pid == pid
    ):
        return
    now = utcnow()
    task.runtime.pipeline.updated_at = now
    task.runtime.execution.active_subagent = task.runtime.execution.active_subagent.model_copy(
        update={"pid": pid, "updated_at": now}
    )
    save_task_runtime(root, task)


def mark_subagent_progress(
    root: Path,
    task: TaskRecord,
    *,
    pid: int | None = None,
    transcript: str | None = None,
    continuation: RuntimeEngineContinuation | None = None,
) -> None:
    if task.runtime.execution.active_subagent is None:
        return
    now = utcnow()
    task.runtime.pipeline.updated_at = now
    if task.runtime.pipeline.current_stage.stage is not None:
        task.runtime.pipeline.current_stage = task.runtime.pipeline.current_stage.model_copy(update={"updated_at": now})
    updates: dict[str, object] = {"updated_at": now}
    if pid is not None:
        updates["pid"] = pid
    if transcript is not None:
        updates["transcript_snippet"] = summarize_transcript(transcript)
    if continuation is not None:
        updates["continuation"] = continuation
    task.runtime.execution.active_subagent = task.runtime.execution.active_subagent.model_copy(update=updates)
    save_task_runtime(root, task)


def mark_subagent_finished(
    root: Path,
    task: TaskRecord,
    ref: SubagentRef,
    transcript: str,
    exit_code: int,
    pid: int | None = None,
    interruption_reason: str | None = None,
    continuation: RuntimeEngineContinuation | None = None,
) -> None:
    now = utcnow()
    started_at = task.runtime.execution.active_subagent.started_at if task.runtime.execution.active_subagent else now
    runtime_pid = pid
    if runtime_pid is None and task.runtime.execution.active_subagent is not None:
        runtime_pid = task.runtime.execution.active_subagent.pid
    task.runtime.pipeline.updated_at = now
    task.runtime.execution.last_subagent = _runtime_subagent_state(
        ref,
        started_at=started_at,
        updated_at=now,
        pid=runtime_pid,
        completed_at=now,
        exit_code=exit_code,
        transcript_snippet=summarize_transcript(transcript),
        interruption_reason=interruption_reason or "",
        continuation=(
            continuation
            if continuation is not None
            else task.runtime.execution.active_subagent.continuation
            if task.runtime.execution.active_subagent is not None
            else None
        ),
    )
    task.runtime.execution.active_subagent = None
    save_task_runtime(root, task)


def mark_engine_switch(
    root: Path,
    task: TaskRecord,
    *,
    stage: str,
    from_engine: str,
    to_engine: str,
    reason: str,
) -> None:
    now = utcnow()
    task.runtime.pipeline.updated_at = now
    task.runtime.execution.last_engine_switch = RuntimeEngineSwitch(
        stage=stage,
        from_engine=from_engine,
        to_engine=to_engine,
        reason=reason,
        happened_at=now,
    )
    save_task_runtime(root, task)


def summarize_transcript(transcript: str, limit: int = 120) -> str:
    for line in transcript.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("VERDICT:"):
            continue
        if stripped.startswith("SUMMARY:"):
            stripped = stripped.partition(":")[2].strip()
        return stripped if len(stripped) <= limit else stripped[: limit - 3].rstrip() + "..."
    return ""


def duration_seconds(started_at: str | None, ended_at: str | None) -> int:
    if started_at is None or ended_at is None:
        return 0
    try:
        from datetime import datetime

        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at)
    except ValueError:
        return 0
    return max(0, int((ended - started).total_seconds()))
