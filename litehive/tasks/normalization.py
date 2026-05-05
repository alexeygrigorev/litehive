"""Normalization and validation helpers for task fields."""

import re

from litehive.domain.common import PipelineStatus, TaskStage
from litehive.domain.task import TaskRecord


def normalize_acceptance_criteria(items: list[str] | None) -> list[str]:
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
    return normalize_acceptance_criteria(items)


def extract_report_line(text: str, key: str) -> str | None:
    match = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None


def extract_report_list_section(text: str, key: str) -> list[str]:
    items: list[str] = []
    capture = False
    header = f"{key}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == header:
            capture = True
            continue
        if capture and re.match(r"^[A-Z_]+:", stripped):
            break
        if capture and line.lstrip().startswith("- "):
            items.append(line.split("- ", 1)[1].strip())
    return items


def task_requires_acceptance_criteria(task: TaskRecord) -> bool:
    return bool(_acceptance_criteria_requirement_signals(task))


def missing_acceptance_criteria_reason(task: TaskRecord) -> str | None:
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
    reason = missing_acceptance_criteria_reason(task)
    if reason is None:
        return None
    return (
        f"{reason} This task will stay in `grooming` until criteria are added. "
        "Use `--acceptance-criteria` to persist at least one structured bullet."
    )


def implementation_entry_stage(task: TaskRecord) -> str:
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
    if getattr(task, "pipeline_mode", "full") == "single":
        return task.pipeline_status
    if task.pipeline_status in _ACCEPTANCE_REROUTE_STATUSES:
        if missing_acceptance_criteria_reason(task) is not None:
            return TaskStage.GROOMING.value
        return task.pipeline_status
    return task.pipeline_status


def _acceptance_criteria_requirement_signals(task: TaskRecord) -> list[str]:
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


def infer_acceptance_criteria(task: TaskRecord) -> list[str]:
    if task.acceptance_criteria:
        return list(task.acceptance_criteria)

    inferred: list[str] = []
    anchored = False
    goal = task.goal.strip()
    if goal and goal != task.title.strip():
        inferred.append(f"The delivered change achieves the stated goal: {goal}.")
        anchored = True

    if task.plan:
        if len(task.plan) == 1:
            inferred.append(f"The implementation completes the planned work: {task.plan[0]}.")
        else:
            inferred.append("The implementation covers the defined plan end-to-end without broad unrelated changes.")
        anchored = True

    if task.depends_on and anchored:
        dependency_list = ", ".join(task.depends_on)
        inferred.append(f"The result aligns with the prerequisite task context needed from: {dependency_list}.")

    if not inferred:
        return []

    inferred.append("Focused verification demonstrates the targeted behavior works as intended.")
    return normalize_acceptance_criteria(inferred)
