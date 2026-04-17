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
    if task.status != "interrupted" or task.runtime.current_stage.status != "interrupted":
        return ""
    interruption = task.runtime.interruption
    stage = (
        interruption.resume_stage
        if interruption is not None and interruption.resume_stage is not None
        else task.runtime.current_stage.stage or task.pipeline_status
    )
    label = f" resumable_from={stage}"
    if interruption is not None:
        label += f" interruption={interruption.source}"
    if task.runtime.last_outcome.reason_code:
        label += f" reason_code={task.runtime.last_outcome.reason_code}"
    if task.runtime.last_subagent is not None:
        label += (
            " last_subagent="
            f"{task.runtime.last_subagent.id}:{task.runtime.last_subagent.role}/{task.runtime.last_subagent.engine}"
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
