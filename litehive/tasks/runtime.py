"""Runtime state tracking: runs, stages, subagents, engine switches."""

from collections.abc import Callable

from litehive.domain.common import (
    PipelineStatus,
    RuntimeStageStatus,
    TaskExecutionStatus,
    TaskStatus,
    utcnow,
)
from litehive.domain.failure_diagnostics import FailureDiagnosticValue, FailureDiagnostics
from litehive.domain.outcomes import OutcomeReasonCode, TaskOutcomeKind
from litehive.domain.reports import StageReport
from litehive.domain.runtime import (
    RuntimeEngineContinuation,
    RuntimeEngineSwitch,
    RuntimeStageState,
    Subagent,
    RuntimeSubagentState,
    TaskOutcomeState,
)
from litehive.domain.task import TaskRecord

from litehive.state.records import (
    WorkspaceTasks,
    WorkspaceTasks,
)
from litehive.state.locking import WorkspaceMutationGuard, WorkspaceStateLock
from litehive.state.persist import WorkspaceStateRepository
from litehive.workspace import Workspace


def idle_stage_state(updated_at: str, stage: str | None = None) -> RuntimeStageState:
    """
    Build the between-stages runtime marker.

    Returned after a stage finishes or a run resets, so observers see an
    explicit ``idle`` snapshot rather than inferring idleness from the
    absence of a ``running`` marker.
    """
    return RuntimeStageState(stage=stage, status=RuntimeStageStatus.IDLE, updated_at=updated_at)


def _running_stage_state(stage: str, started_at: str) -> RuntimeStageState:
    """
    Build the runtime marker that says a stage is currently executing.

    Used by ``mark_stage_started`` to seed ``runtime.pipeline.current_stage``
    so status surfaces and the operator UI can show "running ``<stage>``"
    until the stage finishes and resets back to ``idle``.
    """
    return RuntimeStageState(
        stage=stage,
        status=RuntimeStageStatus.RUNNING,
        started_at=started_at,
        updated_at=started_at,
    )


def _runtime_subagent_state(
    subagent: Subagent,
    started_at: str,
    updated_at: str,
    pid: int | None = None,
    completed_at: str | None = None,
    exit_code: int | None = None,
    execution_trace_snippet: str = "",
    interruption_reason: str = "",
    continuation: RuntimeEngineContinuation | None = None,
) -> RuntimeSubagentState:
    """
    Project a persisted ``Subagent`` plus run-time fields into
    ``RuntimeSubagentState``.

    The single helper used by ``mark_subagent_started`` /
    ``mark_subagent_progress`` / ``mark_subagent_finished`` so every
    subagent state transition shares the same field mapping; adding a new
    subagent field only requires editing one place.
    """
    return RuntimeSubagentState(
        id=subagent.id,
        role=subagent.role,
        engine=subagent.engine,
        status=subagent.status,
        path=subagent.path,
        pid=pid,
        sandboxed=subagent.sandboxed,
        sandbox_summary=subagent.sandbox_summary,
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
    execution_status: TaskExecutionStatus | str,
    updated_at: str | None = None,
    clear_interruption: bool = False,
) -> str:
    """
    Wipe per-run runtime fields and set the new ``execution_status``.

    Lets every transition (start, finish, park, abandon, recover) move the
    task into a clean execution status without leaving stale subagent or
    run-start data behind that would confuse the next pickup.
    """
    now = updated_at or utcnow()
    task.runtime.pipeline.execution_status = (
        execution_status
        if isinstance(execution_status, TaskExecutionStatus)
        else TaskExecutionStatus(execution_status)
    )
    task.runtime.pipeline.run_started_at = None
    task.runtime.pipeline.updated_at = now
    task.runtime.execution.active_subagent = None
    if clear_interruption:
        task.runtime.execution.interruption = None
    return now


class TaskRuntimeTransitions:
    """
    Workspace-bound service for task runtime state transitions.
    """

    def __init__(
        self,
        workspace: Workspace,
        tasks: WorkspaceTasks,
        clock: Callable[[], str] = utcnow,
    ) -> None:
        self.workspace = workspace
        self.tasks = tasks
        self.clock = clock

    def clear_run_activity(
        self,
        task: TaskRecord,
        execution_status: TaskExecutionStatus | str,
        updated_at: str | None = None,
        clear_interruption: bool = False,
    ) -> str:
        return clear_task_run_activity(
            task,
            execution_status=execution_status,
            updated_at=updated_at or self.clock(),
            clear_interruption=clear_interruption,
        )

    def start_run(self, task: TaskRecord) -> None:
        self._apply_task_run_started(task)
        self.tasks.save_runtime(task)

    def finish_run(self, task: TaskRecord, final_status: TaskExecutionStatus | str) -> None:
        self._apply_task_run_finished(task, final_status)
        self.tasks.save_runtime(task)

    def finish_run_transition(self, task: TaskRecord, final_status: TaskExecutionStatus | str) -> TaskRecord:
        with WorkspaceMutationGuard(self.workspace).hold(), WorkspaceStateLock(self.workspace).hold():
            canonical_final_status = (
                final_status if isinstance(final_status, TaskExecutionStatus) else TaskExecutionStatus(final_status)
            )
            self._apply_flag_count_auto_defer(task)
            self.clear_run_activity(task, execution_status=canonical_final_status)
            state = WorkspaceStateRepository(self.workspace).load()
            state_changed = False
            if state.active_task_id == task.id:
                state.active_task_id = None
                state_changed = True
            queued_without_task = [item for item in state.queue if item != task.id]
            if queued_without_task != state.queue:
                state.queue = queued_without_task
                state_changed = True
            if (
                canonical_final_status
                in {TaskExecutionStatus.PAUSED, TaskExecutionStatus.QUEUED, TaskExecutionStatus.INTERRUPTED}
                and task.status == TaskStatus.QUEUED
                and task.pipeline_status != PipelineStatus.DONE
            ):
                state.queue.insert(0, task.id)
                state_changed = True
            if (
                canonical_final_status == TaskExecutionStatus.DONE
                and task.status == TaskStatus.DONE
                and task.pipeline_status == PipelineStatus.DONE
                and not state_changed
            ):
                self.tasks.write_runtime(task)
                return task
            WorkspaceStateRepository(self.workspace).persist_task_and_state(task=task, state=state)
            return task

    def set_retry_state(self, task: TaskRecord, retry_count: int, retry_limit: int) -> None:
        _apply_task_retry_state(task, retry_count=retry_count, retry_limit=retry_limit)
        self.tasks.save_runtime(task)

    def clear_outcome(self, task: TaskRecord) -> None:
        _clear_task_outcome(task)
        self.tasks.save_runtime(task)

    def record_outcome(
        self,
        task: TaskRecord,
        kind: TaskOutcomeKind | str,
        stage: str,
        reason_code: OutcomeReasonCode | str,
        reason: str,
        retry_count: int,
        retry_limit: int,
        follow_up_task_id: str | None = None,
        failure_classification: str | None = None,
        failure_diagnostics: FailureDiagnostics | dict[str, FailureDiagnosticValue] | None = None,
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
        self.tasks.save_runtime(task)

    def start_stage(self, task: TaskRecord, stage: str) -> None:
        self._apply_stage_started(task, stage)
        self.tasks.save_runtime(task)

    def finish_stage(self, task: TaskRecord, report: StageReport) -> None:
        del report
        self._apply_stage_finished(task)
        self.tasks.save_runtime(task)

    def mark_subagent_started(self, task: TaskRecord, ref: Subagent) -> None:
        self._apply_subagent_started(task, ref)
        self.tasks.save_runtime(task)

    def mark_subagent_pid(self, task: TaskRecord, pid: int | None) -> None:
        if not self._apply_subagent_pid(task, pid):
            return
        self.tasks.save_runtime(task)

    def mark_subagent_progress(
        self,
        task: TaskRecord,
        pid: int | None = None,
        transcript: str | None = None,
        continuation: RuntimeEngineContinuation | None = None,
    ) -> None:
        if not self._apply_subagent_progress(task, pid=pid, transcript=transcript, continuation=continuation):
            return
        self.tasks.save_runtime(task)

    def mark_subagent_finished(
        self,
        task: TaskRecord,
        ref: Subagent,
        transcript: str,
        exit_code: int,
        pid: int | None = None,
        interruption_reason: str | None = None,
        continuation: RuntimeEngineContinuation | None = None,
    ) -> None:
        del ref, transcript, exit_code, pid, interruption_reason, continuation
        self._apply_subagent_finished(task)
        self.tasks.save_runtime(task)

    def switch_engine(
        self,
        task: TaskRecord,
        stage: str,
        from_engine: str,
        to_engine: str,
        reason: str,
    ) -> None:
        self._apply_engine_switch(
            task,
            stage=stage,
            from_engine=from_engine,
            to_engine=to_engine,
            reason=reason,
        )
        self.tasks.save_runtime(task)

    def _apply_task_run_started(self, task: TaskRecord) -> None:
        now = self.clear_run_activity(task, execution_status=TaskExecutionStatus.RUNNING, clear_interruption=True)
        task.runtime.pipeline.run_started_at = now
        task.runtime.pipeline.retry_count = 0
        task.runtime.pipeline.retry_limit = task.runtime.pipeline.retry_limit
        task.runtime.pipeline.last_outcome = TaskOutcomeState()
        task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now)

    def _apply_task_run_finished(self, task: TaskRecord, final_status: TaskExecutionStatus | str) -> None:
        self.clear_run_activity(task, execution_status=final_status)

    def _apply_flag_count_auto_defer(self, task: TaskRecord) -> None:
        if task.status != TaskStatus.FLAGGED:
            return
        task.flag_count += 1
        if task.flag_count >= 3:
            task.flag_reason = "flagged 3 times - needs human review"

    def _apply_stage_started(self, task: TaskRecord, stage: str) -> None:
        now = self.clock()
        task.runtime.pipeline.updated_at = now
        task.runtime.pipeline.current_stage = _running_stage_state(stage, started_at=now)

    def _apply_stage_finished(self, task: TaskRecord) -> None:
        now = self.clock()
        task.runtime.pipeline.updated_at = now
        task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now)

    def _apply_subagent_started(self, task: TaskRecord, ref: Subagent) -> None:
        now = self.clock()
        task.runtime.pipeline.updated_at = now
        task.runtime.execution.active_subagent = _runtime_subagent_state(ref, started_at=now, updated_at=now)

    def _apply_subagent_pid(self, task: TaskRecord, pid: int | None) -> bool:
        active_subagent = task.runtime.execution.active_subagent
        if pid is None or active_subagent is None or active_subagent.pid == pid:
            return False
        now = self.clock()
        task.runtime.pipeline.updated_at = now
        task.runtime.execution.active_subagent = active_subagent.model_copy(update={"pid": pid, "updated_at": now})
        return True

    def _apply_subagent_progress(
        self,
        task: TaskRecord,
        pid: int | None = None,
        transcript: str | None = None,
        continuation: RuntimeEngineContinuation | None = None,
    ) -> bool:
        if task.runtime.execution.active_subagent is None:
            return False
        now = self.clock()
        task.runtime.pipeline.updated_at = now
        if task.current_pipeline_stage is not None:
            task.runtime.pipeline.current_stage = task.runtime.pipeline.current_stage.model_copy(update={"updated_at": now})
        updates: dict[str, object] = {"updated_at": now}
        if pid is not None:
            updates["pid"] = pid
        if transcript is not None:
            updates["execution_trace_snippet"] = summarize_transcript(transcript)
        if continuation is not None:
            updates["continuation"] = continuation
        task.runtime.execution.active_subagent = task.runtime.execution.active_subagent.model_copy(update=updates)
        return True

    def _apply_subagent_finished(self, task: TaskRecord) -> None:
        now = self.clock()
        task.runtime.pipeline.updated_at = now
        task.runtime.execution.active_subagent = None

    def _apply_engine_switch(
        self,
        task: TaskRecord,
        stage: str,
        from_engine: str,
        to_engine: str,
        reason: str,
    ) -> None:
        now = self.clock()
        task.runtime.pipeline.updated_at = now
        task.runtime.execution.last_engine_switch = RuntimeEngineSwitch(
            stage=stage,
            from_engine=from_engine,
            to_engine=to_engine,
            reason=reason,
            happened_at=now,
        )



def _apply_task_retry_state(
    task: TaskRecord,
    retry_count: int,
    retry_limit: int,
) -> None:
    """
    In-memory half of ``set_task_retry_state``.

    Bumps retry counters and the ``updated_at`` marker without persisting,
    so ``apply_task_outcome`` can update retry numbers as part of a larger
    atomic in-memory mutation without triggering its own disk write.
    """
    task.runtime.pipeline.updated_at = utcnow()
    task.runtime.pipeline.retry_count = retry_count
    task.runtime.pipeline.retry_limit = retry_limit


def _clear_task_outcome(task: TaskRecord) -> None:
    """
    In-memory half of ``clear_task_outcome``.

    Zeroes ``last_outcome`` without persisting so ``apply_task_outcome``
    and the recovery flow can scrub stale verdicts as part of a larger
    mutation persisted in one transaction by the caller.
    """
    task.runtime.pipeline.updated_at = utcnow()
    task.runtime.pipeline.last_outcome = TaskOutcomeState()


def apply_task_outcome(
    task: TaskRecord,
    kind: TaskOutcomeKind | str,
    stage: str,
    reason_code: OutcomeReasonCode | str,
    reason: str,
    retry_count: int,
    retry_limit: int,
    follow_up_task_id: str | None = None,
    failure_classification: str | None = None,
    failure_diagnostics: FailureDiagnostics | dict[str, FailureDiagnosticValue] | None = None,
) -> None:
    """
    In-memory variant of ``mark_task_outcome``.

    Called by transitions (close, abandon, recovery) that already hold the
    workspace lock and want to bundle the outcome into their own
    persistence batch; persisting twice would re-emit the audit/event log
    entries the batch already covers.
    """
    now = utcnow()
    outcome_kind = kind if isinstance(kind, TaskOutcomeKind) else TaskOutcomeKind(kind)
    outcome_reason_code = (
        reason_code if isinstance(reason_code, OutcomeReasonCode) else OutcomeReasonCode(reason_code)
    )
    task.runtime.pipeline.updated_at = now
    task.runtime.pipeline.last_outcome = TaskOutcomeState(
        kind=outcome_kind,
        stage=stage,
        reason_code=outcome_reason_code,
        reason=reason,
        failure_classification=failure_classification,
        failure_diagnostics=_normalize_failure_diagnostics(failure_diagnostics),
        follow_up_task_id=follow_up_task_id,
        retry_count=retry_count,
        retry_limit=retry_limit,
        recorded_at=now,
    )


def _normalize_failure_diagnostics(
    failure_diagnostics: FailureDiagnostics | dict[str, FailureDiagnosticValue] | None,
) -> FailureDiagnostics:
    """
    Convert legacy dict inputs into the typed diagnostics value.
    """
    if failure_diagnostics is None:
        return FailureDiagnostics({})
    if isinstance(failure_diagnostics, FailureDiagnostics):
        return failure_diagnostics
    return FailureDiagnostics(failure_diagnostics)


def summarize_transcript(transcript: str, limit: int = 120) -> str:
    """
    Pick a short human-readable line from a subagent transcript.

    Used by the status snapshot's "current activity" field; skips the
    structured ``VERDICT:``/``SUMMARY:`` scaffolding so operators see real
    progress text rather than the prompt formalisms the agent is required
    to emit.
    """
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
    """
    Compute an integer second delta between two ISO timestamps.

    Tolerates missing or malformed inputs by returning ``0`` rather than
    raising; status formatting uses this so a missing or corrupt timestamp
    cannot crash the operator-facing report mid-render.
    """
    if started_at is None or ended_at is None:
        return 0
    try:
        from datetime import datetime  # noqa: PLC0415

        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at)
    except ValueError:
        return 0
    return max(0, int((ended - started).total_seconds()))
