from litehive.domain.common import TaskStatus


def format_engine_int_map(values):
    if not values:
        return "-"
    return ", ".join(f"{engine}={limit}" for engine, limit in sorted(values.items()))


def format_retry_on(config):
    if not config.retry_on:
        return "-"
    return ",".join(config.retry_on)


def task_engine_label(task_engine, default_engine):
    return task_engine or f"{default_engine} (default)"


def task_model_label(task_model):
    return task_model or "default"


def task_dependencies_label(task_id, dependencies):
    if not dependencies:
        return "-"
    return ", ".join(dependency_id for dependency_id in dependencies if dependency_id != task_id) or "-"


def task_interruption_label(task):
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


def cli_override_or_default(value, default):
    if value is None:
        return default
    if isinstance(default, bool) and value is False:
        return default
    return value
