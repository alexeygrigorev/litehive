"""Normalization and validation helpers for task fields."""

from litehive.domain.common import PipelineStatus, TaskStage
from litehive.domain.task import TaskRecord


def normalize_acceptance_criteria(items: list[str] | None) -> list[str]:
    """
    Strip whitespace and drop blank entries from acceptance-criteria input.

    The canonical scrubber every loader and CLI parser funnels into, so
    persisted criteria are never empty strings or padded duplicates that
    would later confuse the grooming gate's "has criteria?" check.
    """
    if not items:
        return []

    normalized: list[str] = []
    for item in items:
        criterion = item.strip()
        if not criterion:
            continue
        normalized.append(criterion)
    return normalized


def normalize_task_text_list(items: list[str] | None) -> list[str]:
    """
    Generic alias for the criteria scrubber.

    Kept under a generic name so ``--constraints`` and ``--plan`` parsers can
    apply the same rule without importing a criteria-specific symbol; the
    name (not the body) is the contract that lets the alias evolve later.
    """
    return normalize_acceptance_criteria(items)


def missing_acceptance_criteria_reason(task: TaskRecord) -> str | None:
    """
    Return a human-readable explanation when criteria are required but absent.

    The canonical "why is this task stuck in grooming?" message reused by
    queue commands and the pipeline reroute that bounces an under-specified
    task back to grooming, so the same wording reaches every operator
    surface.
    """
    if task.acceptance_criteria:
        return None
    signals = _acceptance_criteria_requirement_signals(task)
    if not signals:
        return None
    return (
        "Structured acceptance criteria are required before implementation for larger tasks. "
        f"Add at least one criterion because this task has: {', '.join(signals)}."
    )


def missing_acceptance_criteria_cli_warning(task: TaskRecord) -> str | None:
    """
    Wrap the missing-criteria reason with the actionable ``--acceptance-criteria`` hint.

    Used by ``task add`` and ``task update`` so the operator is nudged to fix
    the task in place rather than discovering it stuck in grooming several
    pipeline ticks later.
    """
    reason = missing_acceptance_criteria_reason(task)
    if reason is None:
        return None
    return (
        f"{reason} This task will stay in `grooming` until criteria are added. "
        "Use `--acceptance-criteria` to persist at least one structured bullet."
    )


def implementation_entry_stage(task: TaskRecord) -> str:
    """
    Decide which stage a freshly (re-)queued task should re-enter at.

    Implementing for single-mode and groomed full-mode tasks; grooming for
    full-mode tasks still missing criteria. Used by queue and recovery flows
    so requeue cannot bypass the grooming gate by sending an under-specified
    task straight back to implementation.
    """
    if getattr(task, "pipeline_mode", "full") == "single":
        return TaskStage.IMPLEMENTING.value
    if missing_acceptance_criteria_reason(task) is not None:
        return TaskStage.GROOMING.value
    return TaskStage.IMPLEMENTING.value


def needs_normalization(task: TaskRecord) -> str | None:
    """
    Return a reason if the task needs planner normalization before retry.

    Tasks already at backlog or grooming will go through planner naturally,
    so normalization only applies to tasks past grooming that lack acceptance
    criteria. Single-mode tasks skip normalization entirely because they
    have no grooming stage to bounce back to.
    """
    if getattr(task, "pipeline_mode", "full") == "single":
        return None
    if task.pipeline_status in {PipelineStatus.BACKLOG, PipelineStatus.GROOMING}:
        return None
    if task.acceptance_criteria:
        return None
    reasons = ["missing acceptance criteria"]
    if not task.goal.strip():
        reasons.append("missing goal")
    return f"Task is underspecified ({', '.join(reasons)}) and needs planner normalization before retry."


_ACCEPTANCE_REROUTE_STATUSES = frozenset(
    {
        PipelineStatus.IMPLEMENTING,
        PipelineStatus.TESTING,
        PipelineStatus.ACCEPTING,
        PipelineStatus.COMMIT_TO_GIT,
    }
)


def reroute_stage_for_acceptance_criteria(task: TaskRecord) -> str:
    """
    Reroute a stage-reached task back to grooming when criteria are missing.

    Called from the resume and update transitions so an under-specified task
    that somehow reached implementation/testing/accepting/commit cannot slip
    past the grooming gate; returns the original stage when criteria are
    present so the call site is safe to invoke unconditionally.
    """
    if getattr(task, "pipeline_mode", "full") == "single":
        return task.pipeline_status
    if task.pipeline_status in _ACCEPTANCE_REROUTE_STATUSES:
        if missing_acceptance_criteria_reason(task) is not None:
            return TaskStage.GROOMING.value
        return task.pipeline_status
    return task.pipeline_status


def _acceptance_criteria_requirement_signals(task: TaskRecord) -> list[str]:
    """
    Collect the human-readable signals that say "this task needs criteria".

    Shared by ``missing_acceptance_criteria_reason`` and the CLI warning so
    the rule (dependencies, explicit goal, high priority, multi-step plan)
    lives in one place — adding a new signal only requires editing this
    helper.
    """
    signals: list[str] = []
    if task.depends_on:
        signals.append("dependencies")
    if task.goal.strip() and task.goal.strip() != task.title.strip():
        signals.append("an explicit goal")
    if task.priority == "high":
        signals.append("high priority")
    if len(task.plan) >= 2:
        signals.append("a multi-step plan")
    return signals


