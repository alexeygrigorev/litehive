"""Normalization and validation helpers for task fields."""

from litehive.domain.common import PipelineStatus, TaskStage
from litehive.domain.task import TaskRecord


def normalize_acceptance_criteria(items: list[str] | None) -> list[str]:
    """Strip whitespace and drop blank entries from acceptance-criteria input; the canonical scrubber every loader/CLI parser funnels into so persisted criteria are never empty strings or padded duplicates."""
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
    """Public alias for the criteria scrubber, kept under a generic name so the `--constraints`/`--plan` CLI parsers can apply the same rule without depending on the criteria-specific symbol; the indirection is the contract, not the body."""
    return normalize_acceptance_criteria(items)


def missing_acceptance_criteria_reason(task: TaskRecord) -> str | None:
    """Return a human-readable explanation when this task should have structured acceptance criteria but does not; the canonical "why is this task stuck in grooming?" message reused by both queue commands and pipeline rerouting."""
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
    """Wrap the missing-criteria reason with the operator-actionable hint about `--acceptance-criteria`; used by `task add`/`task update` to nudge the user to fix the task in-place rather than discovering it stuck in grooming later."""
    reason = missing_acceptance_criteria_reason(task)
    if reason is None:
        return None
    return (
        f"{reason} This task will stay in `grooming` until criteria are added. "
        "Use `--acceptance-criteria` to persist at least one structured bullet."
    )


def implementation_entry_stage(task: TaskRecord) -> str:
    """Decide which stage a freshly (re-)queued task should re-enter at — implementing for single-mode and groomed full-mode tasks, grooming for full-mode tasks still missing criteria; used by queue/recovery flows so requeue does not bypass the grooming gate."""
    if getattr(task, "pipeline_mode", "full") == "single":
        return TaskStage.IMPLEMENTING.value
    if missing_acceptance_criteria_reason(task) is not None:
        return TaskStage.GROOMING.value
    return TaskStage.IMPLEMENTING.value


def needs_normalization(task: TaskRecord) -> str | None:
    """Return a reason if the task is underspecified and needs planner normalization before retry.

    Tasks already at backlog or grooming will naturally go through planner,
    so normalization only applies to tasks past grooming that lack meaningful
    acceptance criteria.

    Single-mode tasks skip normalization entirely since they have no grooming stage.
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
    """Send a task back to grooming when it has reached implementation/testing/accepting/commit stages without acceptance criteria so resume/repair flows do not let an underspecified task slip past the grooming gate; called by `tasks/status.py` on resume and on rejection."""
    if getattr(task, "pipeline_mode", "full") == "single":
        return task.pipeline_status
    if task.pipeline_status in _ACCEPTANCE_REROUTE_STATUSES:
        if missing_acceptance_criteria_reason(task) is not None:
            return TaskStage.GROOMING.value
        return task.pipeline_status
    return task.pipeline_status


def _acceptance_criteria_requirement_signals(task: TaskRecord) -> list[str]:
    """Collect the human-readable signals that mark a task as requiring structured criteria; shared by the boolean predicate and the explanatory-reason helpers so the rule is defined once."""
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


