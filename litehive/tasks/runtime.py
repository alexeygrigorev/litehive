"""Runtime state tracking: runs, stages, subagents, engine switches."""

from pathlib import Path

from litehive.domain.common import PipelineStatus, TaskStatus, utcnow
from litehive.domain.reports import StageReport
from litehive.domain.runtime import (
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


def idle_stage_state(updated_at: str, stage: str | None = None) -> RuntimeStageState:
    """Build the between-stages runtime marker used after a stage finishes or a run resets."""
    return RuntimeStageState(stage=stage, updated_at=updated_at)


def _running_stage_state(stage: str, started_at: str) -> RuntimeStageState:
    return RuntimeStageState(
        stage=stage,
        status="running",
        started_at=started_at,
        updated_at=started_at,
    )


def _runtime_subagent_state(
    ref: SubagentRef,
    started_at: str,
    updated_at: str,
    pid: int | None = None,
    completed_at: str | None = None,
    exit_code: int | None = None,
    execution_trace_snippet: str = "",
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
        execution_trace_snippet=execution_trace_snippet,
        interruption_reason=interruption_reason,
        continuation=continuation,
    )


def clear_task_run_activity(
    task: TaskRecord,
    execution_status: str,
    updated_at: str | None = None,
    clear_interruption: bool = False,
) -> str:
    """Wipe per-run runtime fields so callers can transition a task into a new execution_status without leaving stale subagent or run-start data behind."""
    now = updated_at or utcnow()
    task.runtime.pipeline.execution_status = execution_status
    task.runtime.pipeline.run_started_at = None
    task.runtime.pipeline.updated_at = now
    task.runtime.execution.active_subagent = None
    if clear_interruption:
        task.runtime.execution.interruption = None
    return now


def mark_task_run_started(root: Path, task: TaskRecord) -> None:
    """Record that the runner has just begun executing this task; called by the orchestration loop when a queued task is picked up."""
    now = clear_task_run_activity(task, execution_status="running", clear_interruption=True)
    task.runtime.pipeline.run_started_at = now
    task.runtime.pipeline.retry_count = 0
    task.runtime.pipeline.retry_limit = task.runtime.pipeline.retry_limit
    task.runtime.pipeline.last_outcome = TaskOutcomeState()
    task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now)
    save_task_runtime(root, task)


def mark_task_run_finished(root: Path, task: TaskRecord, final_status: str) -> None:
    """Persist the closing execution_status for a task without touching the workspace queue; called when the orchestration loop only needs to flush runtime fields, not transition queue ownership."""
    clear_task_run_activity(task, execution_status=final_status)
    save_task_runtime(root, task)


def apply_flag_count_auto_defer(task: TaskRecord) -> None:
    """Increment flag_count and auto-defer if the threshold is reached."""
    if task.status != TaskStatus.FLAGGED:
        return
    task.flag_count += 1
    if task.flag_count >= 3:
        task.flag_reason = "flagged 3 times - needs human review"


def finish_task_run_transition(root: Path, task: TaskRecord, final_status: str) -> TaskRecord:
    """End-of-run transition that reconciles the task and the workspace queue under the workspace lock; called by the orchestration loop when a run terminates (done, paused, interrupted, queued) so flag auto-defer, queue cleanup, and reinsertion all happen atomically."""
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
            and task.status == TaskStatus.QUEUED
            and task.pipeline_status != PipelineStatus.DONE
        ):
            state.queue.insert(0, task.id)
            state_changed = True
        if (
            final_status == "done"
            and task.status == TaskStatus.DONE
            and task.pipeline_status == PipelineStatus.DONE
            and not state_changed
        ):
            write_task_runtime(root, task)
            return task
        persist_task_and_state(root, task=task, state=state)
        return task


def set_task_retry_state(
    root: Path,
    task: TaskRecord,
    retry_count: int,
    retry_limit: int,
) -> None:
    """Persist the current per-stage retry counters; called by stage controllers after a retry so the next prompt and the operator-facing status reflect the same numbers."""
    _apply_task_retry_state(
        task,
        retry_count=retry_count,
        retry_limit=retry_limit,
    )
    save_task_runtime(root, task)


def clear_task_outcome(root: Path, task: TaskRecord) -> None:
    """Reset the last-outcome record so the next stage prompt is not contaminated by a stale verdict; called when a task is being requeued or recovered into a fresh attempt."""
    _clear_task_outcome(task)
    save_task_runtime(root, task)


def _apply_task_retry_state(
    task: TaskRecord,
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
    """Record the verdict that ended a stage (rejected, flagged, deferred, etc.) and flush it to disk; called by stage controllers so downstream prompts and the operator status share one source of truth."""
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
    """In-memory variant of mark_task_outcome; called by transitions (close, abandon, recovery) that already hold the workspace lock and want to bundle the outcome into their own persistence batch."""
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
    """Record that the runner has just entered a pipeline stage; called by the stage dispatcher so observers (status snapshot, daemons) can see what the task is doing right now."""
    now = utcnow()
    task.runtime.pipeline.updated_at = now
    task.runtime.pipeline.current_stage = _running_stage_state(stage, started_at=now)
    save_task_runtime(root, task)


def mark_stage_finished(root: Path, task: TaskRecord, report: StageReport) -> None:
    """Record that a pipeline stage just exited and flush to disk; called by the stage dispatcher so the next stage starts from a clean current_stage marker."""
    apply_stage_finished(task, report)
    save_task_runtime(root, task)


def apply_stage_finished(task: TaskRecord, report: StageReport) -> None:
    """In-memory variant of mark_stage_finished; called by transitions that hold the workspace lock and want to batch the stage-end marker with other writes."""
    now = utcnow()
    task.runtime.pipeline.updated_at = now
    task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now)


def mark_subagent_started(root: Path, task: TaskRecord, ref: SubagentRef) -> None:
    """Attach a freshly launched subagent to the task so observers can see who is running; called when the agent manager spawns a stage subagent."""
    now = utcnow()
    task.runtime.pipeline.updated_at = now
    task.runtime.execution.active_subagent = _runtime_subagent_state(ref, started_at=now, updated_at=now)
    save_task_runtime(root, task)


def mark_subagent_pid(root: Path, task: TaskRecord, pid: int | None) -> None:
    """Attach the OS pid to the active subagent record once the engine has spawned its child process so stop/abandon flows can signal it."""
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
    pid: int | None = None,
    transcript: str | None = None,
    continuation: RuntimeEngineContinuation | None = None,
) -> None:
    """Refresh the heartbeat fields on the active subagent (pid, transcript snippet, engine continuation) so liveness probes don't kill a working agent and so resumed runs can pick up where the engine left off."""
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
        updates["execution_trace_snippet"] = summarize_transcript(transcript)
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
    """Detach the active subagent from the task once its process exits so the next stage starts with a clean slot; called by the agent manager after subagent shutdown."""
    now = utcnow()
    task.runtime.pipeline.updated_at = now
    del ref, transcript, exit_code, pid, interruption_reason, continuation
    task.runtime.execution.active_subagent = None
    save_task_runtime(root, task)


def mark_engine_switch(
    root: Path,
    task: TaskRecord,
    stage: str,
    from_engine: str,
    to_engine: str,
    reason: str,
) -> None:
    """Stamp the task with the most recent engine swap so operator status and the audit trail can explain why a stage is running on a different model than the one configured."""
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
    """Pick a short human-readable line from a subagent transcript for the status snapshot, skipping the structured VERDICT/SUMMARY scaffolding so operators see real progress text."""
    for line in transcript.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("VERDICT:"):
            continue
        if stripped.startswith("SUMMARY:"):
            stripped = stripped.partition(":")[2].strip()
        if len(stripped) <= limit:
            return stripped
        return stripped[: limit - 3].rstrip() + "..."
    return ""


def duration_seconds(started_at: str | None, ended_at: str | None) -> int:
    """Compute an integer second delta between two ISO timestamps that tolerates missing or malformed inputs; used by status formatting where a missing duration must not crash the report."""
    if started_at is None or ended_at is None:
        return 0
    try:
        from datetime import datetime  # noqa: PLC0415

        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at)
    except ValueError:
        return 0
    return max(0, int((ended - started).total_seconds()))
