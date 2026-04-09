"""Runtime state tracking: runs, stages, subagents, engine switches."""

from pathlib import Path

from litehive.models import (
    ResourceLimitEvent,
    RuntimeContinuationHandoff,
    RuntimeEngineContinuation,
    RuntimeEngineSwitch,
    RuntimeSubagentState,
    StageReport,
    SubagentRef,
    TaskOutcomeState,
    TaskRecord,
    utcnow,
)

from litehive.tasks.crud import _write_task_runtime, save_task_runtime
from litehive.workspace.locking import _workspace_lock, workspace_mutation_guard
from litehive.tasks.persistence import load_state
from litehive.workspace.workflow import persist_task_and_state


def mark_task_run_started(root: Path, task: TaskRecord) -> None:
    now = utcnow()
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = now
    task.runtime.updated_at = now
    task.runtime.retry_count = 0
    task.runtime.retry_limit = task.runtime.retry_limit
    task.runtime.last_outcome = TaskOutcomeState()
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": None,
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    task.runtime.active_subagent = None
    task.runtime.interruption = None
    save_task_runtime(root, task)


def mark_task_run_finished(root: Path, task: TaskRecord, final_status: str) -> None:
    now = utcnow()
    task.runtime.execution_status = final_status
    task.runtime.updated_at = now
    task.runtime.active_subagent = None
    save_task_runtime(root, task)


def _apply_flag_count_auto_defer(task: TaskRecord) -> None:
    """Increment flag_count and auto-defer if the threshold is reached."""
    if task.status != "flagged":
        return
    task.flag_count += 1
    if task.flag_count >= 3:
        task.status = "deferred"
        task.flag_reason = "flagged 3 times - needs human review"


def finish_task_run_transition(root: Path, task: TaskRecord, final_status: str) -> TaskRecord:
    with workspace_mutation_guard(root), _workspace_lock(root):
        now = utcnow()
        _apply_flag_count_auto_defer(task)
        task.runtime.execution_status = final_status
        task.runtime.updated_at = now
        task.runtime.active_subagent = None
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
        if (
            final_status == "done"
            and task.status == "done"
            and task.pipeline_status == "done"
            and not state_changed
        ):
            _write_task_runtime(root, task)
            return task
        persist_task_and_state(root, task=task, state=state)
        return task


def set_task_retry_state(
    root: Path,
    task: TaskRecord,
    *,
    retry_count: int,
    retry_limit: int,
    retry_source: str,
) -> None:
    _apply_task_retry_state(
        task,
        retry_count=retry_count,
        retry_limit=retry_limit,
        retry_source=retry_source,
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
    task.runtime.updated_at = utcnow()
    task.runtime.continuation_handoff = handoff
    save_task_runtime(root, task)


def clear_task_continuation_handoff(root: Path, task: TaskRecord) -> None:
    if task.runtime.continuation_handoff is None:
        return
    set_task_continuation_handoff(root, task, None)


def _apply_task_retry_state(
    task: TaskRecord,
    *,
    retry_count: int,
    retry_limit: int,
    retry_source: str,
) -> None:
    task.runtime.updated_at = utcnow()
    task.runtime.retry_count = retry_count
    task.runtime.retry_limit = retry_limit
    task.runtime.retry_source = retry_source


def _clear_task_outcome(task: TaskRecord) -> None:
    task.runtime.updated_at = utcnow()
    task.runtime.last_outcome = TaskOutcomeState()


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
    retry_source: str,
    failure_classification: str | None = None,
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] | None = None,
) -> None:
    _apply_task_outcome(
        task,
        kind=kind,
        stage=stage,
        reason_code=reason_code,
        reason=reason,
        retry_count=retry_count,
        retry_limit=retry_limit,
        retry_source=retry_source,
        failure_classification=failure_classification,
        failure_diagnostics=failure_diagnostics,
    )
    save_task_runtime(root, task)


def _apply_task_outcome(
    task: TaskRecord,
    *,
    kind: str,
    stage: str,
    reason_code: str,
    reason: str,
    retry_count: int,
    retry_limit: int,
    retry_source: str,
    failure_classification: str | None = None,
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] | None = None,
) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.last_outcome = TaskOutcomeState(
        kind=kind,
        stage=stage,
        reason_code=reason_code,
        reason=reason,
        failure_classification=failure_classification,
        failure_diagnostics=dict(failure_diagnostics or {}),
        retry_count=retry_count,
        retry_limit=retry_limit,
        retry_source=retry_source,
        recorded_at=now,
    )


def mark_stage_started(root: Path, task: TaskRecord, step: str) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": step,
            "status": "running",
            "started_at": now,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    save_task_runtime(root, task)


def mark_stage_finished(root: Path, task: TaskRecord, report: StageReport) -> None:
    _apply_stage_finished(task, report)
    save_task_runtime(root, task)


def _apply_stage_finished(task: TaskRecord, report: StageReport) -> None:
    now = utcnow()
    started_at = task.runtime.current_stage.started_at
    task.runtime.updated_at = now
    task.runtime.last_stage = task.runtime.last_stage.model_copy(
        update={
            "step": report.step,
            "status": "completed" if report.verdict in {"pass", "accept"} else report.verdict,
            "started_at": started_at,
            "completed_at": now,
            "updated_at": now,
            "duration_seconds": _duration_seconds(started_at, now),
            "verdict": report.verdict,
            "summary": report.summary,
        }
    )
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": None,
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    if (
        task.runtime.continuation_handoff is not None
        and task.runtime.continuation_handoff.step == report.step
    ):
        task.runtime.continuation_handoff = None


def mark_subagent_started(root: Path, task: TaskRecord, ref: SubagentRef) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.active_subagent = RuntimeSubagentState(
        id=ref.id,
        role=ref.role,
        engine=ref.engine,
        status=ref.status,
        path=ref.path,
        sandboxed=ref.sandboxed,
        sandbox_summary=ref.sandbox_summary,
        started_at=now,
        updated_at=now,
        continuation=None,
    )
    save_task_runtime(root, task)


def mark_subagent_pid(root: Path, task: TaskRecord, pid: int | None) -> None:
    if (
        pid is None
        or task.runtime.active_subagent is None
        or task.runtime.active_subagent.pid == pid
    ):
        return
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.active_subagent = task.runtime.active_subagent.model_copy(
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
    if task.runtime.active_subagent is None:
        return
    now = utcnow()
    task.runtime.updated_at = now
    if task.runtime.current_stage.step is not None:
        task.runtime.current_stage = task.runtime.current_stage.model_copy(
            update={"updated_at": now}
        )
    updates: dict[str, object] = {"updated_at": now}
    if pid is not None:
        updates["pid"] = pid
    if transcript is not None:
        updates["transcript_snippet"] = summarize_transcript(transcript)
    if continuation is not None:
        updates["continuation"] = continuation
    task.runtime.active_subagent = task.runtime.active_subagent.model_copy(update=updates)
    save_task_runtime(root, task)


def mark_subagent_finished(
    root: Path,
    task: TaskRecord,
    ref: SubagentRef,
    transcript: str,
    exit_code: int,
    pid: int | None = None,
    interruption_reason: str | None = None,
    resource_limit_event: ResourceLimitEvent | None = None,
    continuation: RuntimeEngineContinuation | None = None,
) -> None:
    now = utcnow()
    started_at = task.runtime.active_subagent.started_at if task.runtime.active_subagent else now
    runtime_pid = pid
    if runtime_pid is None and task.runtime.active_subagent is not None:
        runtime_pid = task.runtime.active_subagent.pid
    task.runtime.updated_at = now
    task.runtime.last_subagent = RuntimeSubagentState(
        id=ref.id,
        role=ref.role,
        engine=ref.engine,
        status=ref.status,
        path=ref.path,
        pid=runtime_pid,
        sandboxed=ref.sandboxed,
        sandbox_summary=ref.sandbox_summary,
        started_at=started_at,
        updated_at=now,
        completed_at=now,
        exit_code=exit_code,
        transcript_snippet=summarize_transcript(transcript),
        interruption_reason=interruption_reason or "",
        resource_limit_event=resource_limit_event,
        continuation=(
            continuation
            if continuation is not None
            else task.runtime.active_subagent.continuation
            if task.runtime.active_subagent is not None
            else None
        ),
    )
    task.runtime.active_subagent = None
    save_task_runtime(root, task)


def mark_engine_switch(
    root: Path,
    task: TaskRecord,
    *,
    step: str,
    from_engine: str,
    to_engine: str,
    reason: str,
) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.last_engine_switch = RuntimeEngineSwitch(
        step=step,
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


def _duration_seconds(started_at: str | None, ended_at: str | None) -> int:
    if started_at is None or ended_at is None:
        return 0
    try:
        from datetime import datetime

        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at)
    except ValueError:
        return 0
    return max(0, int((ended - started).total_seconds()))

