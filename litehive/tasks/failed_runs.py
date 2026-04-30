"""Helpers for cross-run failed-run history projections."""

from pathlib import Path

from litehive.domain.common import utcnow
from litehive.domain.runtime import RuntimeFailedRunRecord
from litehive.domain.task import TaskRecord
from litehive.lifecycle.persistence import FailedRunRecord, SqlitePersistence, TaskNotFound, failed_run_key

FAILED_RUN_REQUEUE_BUDGET = 1


def blocking_failed_run_records(task: TaskRecord) -> list[RuntimeFailedRunRecord]:
    """Return failed-run records that require operator acknowledgement."""

    blocked: list[RuntimeFailedRunRecord] = []
    for record in task.runtime.pipeline.failed_run_history.values():
        if record.count <= FAILED_RUN_REQUEUE_BUDGET:
            continue
        if record.operator_override_count >= record.count:
            continue
        blocked.append(record)
    return sorted(blocked, key=lambda item: (item.stage, item.failure_shape))


def has_blocking_failed_run_history(task: TaskRecord) -> bool:
    return bool(blocking_failed_run_records(task))


def mark_failed_run_operator_override(
    root: Path,
    task: TaskRecord,
    records: list[RuntimeFailedRunRecord] | None = None,
) -> list[dict[str, object]]:
    """Acknowledge blocked failed-run records on authoritative ``TaskState``.

    Runtime records are updated as the storage projection returned to callers,
    but the lifecycle state row remains the owner so reset/requeue cannot leave
    a stale second mutable copy behind.
    """

    now = utcnow()
    acknowledged: list[dict[str, object]] = []
    pipeline_state = None
    try:
        pipeline_state = SqlitePersistence(root).load(task.id)
    except TaskNotFound:
        pipeline_state = None
    for record in records or blocking_failed_run_records(task):
        key = f"{record.stage}:{record.failure_shape}"
        state_record = None if pipeline_state is None else pipeline_state.failed_run_history.get(key)
        if state_record is None and pipeline_state is not None:
            state_record = FailedRunRecord(
                stage=record.stage,
                failure_shape=record.failure_shape,
                count=record.count,
                first_at=record.first_at,
                latest_at=record.latest_at,
                last_reason=record.last_reason,
                source=record.source,
                classification=record.classification,
                retry_limit=record.retry_limit,
                failed_reason=record.failed_reason,
            )
            pipeline_state.failed_run_history[failed_run_key(state_record.stage, state_record.failure_shape)] = (
                state_record
            )
        stored = task.runtime.pipeline.failed_run_history.get(key)
        if stored is None and state_record is None:
            continue
        if state_record is not None:
            state_record.operator_override_count = max(state_record.operator_override_count, state_record.count)
            state_record.last_operator_override_at = now
        if stored is not None:
            stored.operator_override_count = max(stored.operator_override_count, stored.count)
            stored.last_operator_override_at = now
        source = state_record or stored
        assert source is not None
        acknowledged.append(
            {
                "stage": source.stage,
                "failure_shape": source.failure_shape,
                "count": source.count,
                "operator_override_count": source.operator_override_count,
            }
        )
    if pipeline_state is not None and acknowledged:
        SqlitePersistence(root).save(pipeline_state)
    return acknowledged


def failed_run_block_message(task: TaskRecord, records: list[RuntimeFailedRunRecord]) -> str:
    details = "; ".join(
        (f"{record.stage} shape={record.failure_shape} count={record.count} latest_at={record.latest_at or '-'}")
        for record in records
    )
    return (
        f"Task {task.id} repeatedly exhausted the same stage retry budget: {details}. "
        "Use --force to record an operator override and requeue anyway."
    )
