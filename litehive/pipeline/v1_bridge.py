"""Bridge between v1 ``TaskRecord`` (task.yaml) and v2 ``TaskState``.

Two directions:

- ``load_or_initialize(task_id, workspace_root)`` — called when the
  daemon is about to run a task. Looks up the v2 sqlite row; if it
  doesn't exist yet, creates one with ``stage="ready"`` and the
  pipeline mode from the v1 TaskRecord. Returns the v2 ``TaskState``.
- ``sync_back_to_task_record(state, workspace_root)`` — called after
  the ``StateMachineRunner`` finishes a task. Reads the v1 TaskRecord
  and updates its ``status`` / ``pipeline_status`` fields to mirror
  what the v2 state machine decided.

The bridge is intentionally one-way per call: it never reads v2 state
and writes it back to v1 mid-run. The daemon owns the lifecycle — load
before, sync after.
"""

from pathlib import Path

from litehive.models import TaskRecord
from litehive.tasks.crud import get_task, save_task

from .persistence import SqlitePersistence, TaskState
from .types import PipelineMode


class TaskRecordNotFound(LookupError):
    """Raised when the bridge is asked to operate on a missing task."""


# ── v1 → v2 ────────────────────────────────────────────────────────────


def load_or_initialize(
    task_id: str,
    workspace_root: Path,
    *,
    persistence: SqlitePersistence | None = None,
) -> TaskState:
    """Return a v2 ``TaskState`` for ``task_id``, creating the row if needed.

    Reads the v1 ``TaskRecord`` to pull the task's pipeline mode, which
    the v2 state machine doesn't store anywhere else yet.
    """
    task_record = get_task(workspace_root, task_id)
    if task_record is None:
        raise TaskRecordNotFound(f"no task.yaml row for {task_id!r}")

    pipeline_mode = _map_v1_pipeline_mode(task_record)
    store = persistence or SqlitePersistence(workspace_root)
    return store.initialize(task_id, pipeline_mode=pipeline_mode)


# ── v2 → v1 ────────────────────────────────────────────────────────────


_IN_FLIGHT_STAGE_TO_V1_PIPELINE: dict[str, str] = {
    "ready": "backlog",
    "recovering_pre_exec": "backlog",
    "before_grooming": "grooming",
    "grooming": "grooming",
    "after_grooming": "grooming",
    "before_implementing": "implementing",
    "implementing": "implementing",
    "after_implementing": "implementing",
    "before_testing": "testing",
    "testing": "testing",
    "after_testing": "testing",
    "before_accepting": "accepting",
    "accepting": "accepting",
    "after_accepting": "accepting",
    "before_commit": "commit_to_git",
    "commit": "commit_to_git",
    "after_commit": "commit_to_git",
    "merge_resolving": "commit_to_git",
    "recovering": "grooming",  # placeholder; recovery is cross-cutting
}


def sync_back_to_task_record(
    state: TaskState,
    workspace_root: Path,
) -> TaskRecord | None:
    """Mirror ``state.stage`` back to the v1 TaskRecord's status + pipeline_status.

    Returns the updated ``TaskRecord``, or ``None`` if there's no v1 row
    for this task (in which case the v2 runner was driving a
    v2-only task and there's nothing to sync).
    """
    task_record = get_task(workspace_root, state.task_id)
    if task_record is None:
        return None

    # Terminal stages map to terminal v1 statuses
    if state.stage == "done":
        task_record.status = "done"
        task_record.pipeline_status = "done"
    elif state.stage == "failed":
        # Distinguish merge_failed from general flagged so `litehive status`
        # still shows the merge-conflict path clearly.
        commit_stages = {"commit", "before_commit", "after_commit", "merge_resolving"}
        if state.origin_stage in commit_stages and state.failed_reason in {
            "recovery_crashed",
            "recovery_exhausted",
        }:
            task_record.status = "merge_failed"
            task_record.pipeline_status = "merge_failed"
        else:
            task_record.status = "flagged"
            task_record.pipeline_status = "merge_failed"
    else:
        # In-flight: keep the task marked in_progress and sync pipeline_status
        task_record.status = "in_progress"
        task_record.pipeline_status = _IN_FLIGHT_STAGE_TO_V1_PIPELINE.get(
            state.stage, task_record.pipeline_status
        )

    save_task(workspace_root, task_record)
    return task_record


# ── helpers ────────────────────────────────────────────────────────────


def _map_v1_pipeline_mode(task_record: TaskRecord) -> PipelineMode:
    raw = getattr(task_record, "pipeline_mode", None)
    if raw is None:
        return PipelineMode.FULL
    # v1 TaskRecord.pipeline_mode is a Literal str "single" | "full"; enum it.
    if isinstance(raw, str):
        return PipelineMode(raw)
    return PipelineMode(getattr(raw, "value", "full"))
