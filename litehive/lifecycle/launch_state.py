"""Bridge a queued ``TaskRecord`` into a coherent lifecycle ``TaskState``.

When the runner picks up a task, the sqlite-backed ``TaskState`` row may
not exist yet, may be a stale leftover from a prior run, or may be
mid-flight. These helpers decide whether to create, load, or reset that
row before the state machine starts driving stages.
"""

from pathlib import Path

from litehive.domain.common import (
    PipelineState,
    PipelineStatus,
    TaskStage,
    canonical_pipeline_state,
)
from litehive.domain.task import TaskRecord
from litehive.state.records import get_task

from .persistence import FailedRunRecord, SqlitePersistence, TaskNotFound, TaskState
from .types import PipelineMode


def _load_or_initialize(task_id: str, workspace_root: Path, persistence: SqlitePersistence) -> TaskState:
    """Return a ``TaskState`` for ``task_id``, creating the row if needed."""
    task_record = get_task(workspace_root, task_id)
    if task_record is None:
        raise LookupError(f"no task record for {task_id!r}")
    raw = task_record.pipeline_mode
    if isinstance(raw, str) and raw:
        mode = PipelineMode(raw)
    else:
        mode = PipelineMode.FULL
    entry_stage = _entry_stage_for_task(task_record)
    fresh_state_kwargs = dict(
        task_id=task_id,
        pipeline_mode=mode,
        stage=PipelineState.READY,
        entry_stage=entry_stage,
    )

    def _fresh_state(
        failed_run_history: dict[str, FailedRunRecord] | None = None,
        recovery_history: list | None = None,
        recovery_budget_history_start: int = 0,
    ) -> TaskState:
        """Local closure: build and persist a fresh ``TaskState`` from the captured task fields.

        Closes over ``fresh_state_kwargs`` and ``persistence`` so both the
        no-prior-state and the reset-stale-state branches build identical
        rows; the optional kwargs let the reset branch carry forward the
        prior failure history.
        """
        state = TaskState(
            **fresh_state_kwargs,
            recovery_history=[] if recovery_history is None else recovery_history,
            recovery_budget_history_start=recovery_budget_history_start,
            failed_run_history={} if failed_run_history is None else failed_run_history,
            limits=persistence.limits,
        )
        persistence.save(state)
        return state

    if entry_stage is None:
        try:
            return persistence.load(task_id)
        except TaskNotFound:
            return _fresh_state()

    def _initialize_fresh_state() -> TaskState:
        """Local closure: rebuild a stale ``TaskState`` while preserving the prior recovery/failure memory.

        Used by the stale-state branch above: a resumed task whose persisted
        row drifted from the launch request needs a clean re-entry, but its
        ``failed_run_history`` and ``recovery_history`` must survive so the
        recovery agent and budget tracker keep their context across resets.
        """
        preserved_failed_runs: dict[str, FailedRunRecord] = {}
        preserved_recovery_history = []
        recovery_budget_history_start = 0
        try:
            previous_state = persistence.load(task_id)
            preserved_failed_runs = previous_state.failed_run_history
            preserved_recovery_history = previous_state.recovery_history
            recovery_budget_history_start = len(previous_state.recovery_history)
        except TaskNotFound:
            preserved_failed_runs = {}
            preserved_recovery_history = []
            recovery_budget_history_start = 0
        persistence.reset_current_lifecycle_state(task_id, preserve_run_memory=True)
        return _fresh_state(
            failed_run_history=preserved_failed_runs,
            recovery_history=preserved_recovery_history,
            recovery_budget_history_start=recovery_budget_history_start,
        )

    try:
        state = persistence.load(task_id)
    except TaskNotFound:
        return _fresh_state()
    except Exception:
        raise

    if _stale_launch_state_requires_reset(task_record, state, pipeline_mode=mode, entry_stage=entry_stage):
        return _initialize_fresh_state()
    return state


def _entry_stage_for_task(task_record: TaskRecord) -> PipelineState | None:
    """Pick the pipeline stage a resumed task should re-enter on, or ``None`` if there's nothing to resume.

    Terminal/queue statuses (backlog, done, flagged) deliberately collapse to
    ``None`` so the caller starts a fresh pipeline rather than re-running a
    stage on a task that has already left the pipeline.
    """
    stage = (
        task_record.runtime.pipeline.current_stage.stage
        or (
            None
            if task_record.runtime.execution.interruption is None
            else task_record.runtime.execution.interruption.resume_stage
        )
        or task_record.pipeline_status
    )
    if stage in {None, PipelineStatus.BACKLOG, PipelineStatus.DONE, PipelineStatus.FLAGGED}:
        return None
    if stage == TaskStage.COMMIT_TO_GIT:
        return PipelineState.COMMIT
    return canonical_pipeline_state(stage)


def _launch_requires_fresh_pipeline_state(task_record: TaskRecord) -> bool:
    """Detect a queued resume of a previously-paused task so its sqlite state can be rebuilt before launch.

    Skips tasks already mid-flight (``running``); those keep their existing
    pipeline state.
    """
    return _entry_stage_for_task(task_record) is not None and task_record.runtime.pipeline.execution_status != "running"


def _stale_launch_state_requires_reset(
    task_record: TaskRecord,
    state: TaskState,
    pipeline_mode: PipelineMode,
    entry_stage: PipelineState,
) -> bool:
    """Decide whether a resumed task's persisted ``TaskState`` is incoherent with the launch request and must be reset.

    A row whose mode/entry_stage drifted from the resume request would
    otherwise re-enter the wrong stage; treat that as stale and rebuild.
    """
    if not _launch_requires_fresh_pipeline_state(task_record):
        return False
    if state.pipeline_mode != pipeline_mode:
        return True
    return state.stage != PipelineState.READY or state.entry_stage != entry_stage
