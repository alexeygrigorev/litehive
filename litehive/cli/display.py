from litehive.domain.common import RuntimeStageStatus, TaskStatus


def format_retry_on(config):
    """
    Format the ``retry_on`` failure-kind list for the status header.

    Returns ``-`` when retries are disabled so the header stays
    human-readable instead of showing an empty cell that looks like
    a missing field.
    """
    if not config.retry_on:
        return "-"
    return ",".join(config.retry_on)


def task_engine_label(task_engine, default_engine):
    """
    Render the engine cell for queue and status rows.

    Falls back to ``"<default> (default)"`` when the task has not
    pinned an engine so readers can tell pinned-vs-inherited at a
    glance — important when debugging why two tasks behaved
    differently under the same workspace config.
    """
    return task_engine or f"{default_engine} (default)"


def task_model_label(task_model):
    """
    Render the model cell for queue and status rows.

    Returns ``"default"`` when the task inherits the engine's default
    model rather than printing an empty column, keeping the table
    grid aligned and the inheritance visible.
    """
    return task_model or "default"


def task_dependencies_label(task_id, dependencies):
    """
    Format a task's ``depends_on`` list for the queue view.

    Drops any self-reference so a stale self-edge (which can sneak
    in via legacy data) does not surface as a confusing
    ``T-123 -> T-123`` in the displayed dependency cell.
    """
    if not dependencies:
        return "-"
    return ", ".join(dependency_id for dependency_id in dependencies if dependency_id != task_id) or "-"


def task_interruption_label(task):
    """
    Build the trailing interruption suffix for a parked task row.

    Appended to a task line when the task is paused mid-pipeline.
    Collects resume stage, source, reason code, and which subagent
    was running so operators see *why* a task is interrupted on the
    same line as the task itself, without having to drill into
    ``task show``.
    """
    if (
        task.status != TaskStatus.INTERRUPTED
        or task.runtime.pipeline.current_stage.status != RuntimeStageStatus.INTERRUPTED
    ):
        return ""
    interruption = task.runtime.execution.interruption
    if interruption is not None and interruption.resume_stage is not None:
        stage = interruption.resume_stage
    else:
        stage = task.runtime.pipeline.current_stage.stage or task.pipeline_status
    label = f" resumable_from={stage}"
    if interruption is not None:
        label += f" interruption={interruption.source}"
    if task.runtime.pipeline.last_outcome.reason_code:
        label += f" reason_code={task.runtime.pipeline.last_outcome.reason_code}"
    if interruption is None:
        interrupted_subagent = None
    else:
        interrupted_subagent = interruption.subagent
    if interrupted_subagent is not None:
        label += (
            f" interrupted_subagent={interrupted_subagent.id}:{interrupted_subagent.role}/{interrupted_subagent.engine}"
        )
    if interruption is not None and interruption.reason:
        label += f" reason={interruption.reason}"
    return label
