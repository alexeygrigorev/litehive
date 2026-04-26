"""Helpers for cross-run failed-run history on task runtime state."""

from litehive.domain.common import utcnow
from litehive.domain.runtime import RuntimeFailedRunRecord
from litehive.domain.task import TaskRecord

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
    task: TaskRecord,
    records: list[RuntimeFailedRunRecord] | None = None,
) -> list[dict[str, object]]:
    """Acknowledge blocked failed-run records so one explicit requeue can run."""

    now = utcnow()
    acknowledged: list[dict[str, object]] = []
    for record in records or blocking_failed_run_records(task):
        key = f"{record.stage}:{record.failure_shape}"
        stored = task.runtime.pipeline.failed_run_history.get(key)
        if stored is None:
            continue
        stored.operator_override_count = max(stored.operator_override_count, stored.count)
        stored.last_operator_override_at = now
        acknowledged.append(
            {
                "stage": stored.stage,
                "failure_shape": stored.failure_shape,
                "count": stored.count,
                "operator_override_count": stored.operator_override_count,
            }
        )
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
