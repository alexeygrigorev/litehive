from litehive.domain.common import TaskStatus


def format_retry_on(config):
    """Format the `retry_on` failure-kind list for the status header; returns `-` when retries are disabled so the line stays human-readable."""
    if not config.retry_on:
        return "-"
    return ",".join(config.retry_on)


def task_engine_label(task_engine, default_engine):
    """Render the engine cell for queue/status rows; falls back to "<default> (default)" when the task has not pinned an engine so readers can tell pinned vs inherited at a glance."""
    return task_engine or f"{default_engine} (default)"


def task_model_label(task_model):
    """Render the model cell for queue/status rows; returns "default" when the task inherits the engine's default model rather than printing an empty column."""
    return task_model or "default"


def task_dependencies_label(task_id, dependencies):
    """Format a task's `depends_on` list, omitting any self-reference so a stale self-edge does not show up in the queue view."""
    if not dependencies:
        return "-"
    return ", ".join(dependency_id for dependency_id in dependencies if dependency_id != task_id) or "-"


def task_interruption_label(task):
    """Build the trailing interruption suffix appended to a task row when the task is parked mid-pipeline; collects resume stage, source, reason code, and which subagent was running so operators see *why* a task is interrupted on the same line."""
    if task.status != TaskStatus.INTERRUPTED or task.runtime.pipeline.current_stage.status != "interrupted":
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
