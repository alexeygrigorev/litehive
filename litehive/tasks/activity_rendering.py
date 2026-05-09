"""Task activity rendering and retraction helpers."""

from typing import Iterable

from litehive.domain.common import TaskStage, Verdict
from litehive.domain.reports import TaskActivityEntry
from litehive.domain.task import TaskRecord
from litehive.tasks.activity import task_activity_store_for_task
from litehive.workspace import Workspace

RETRACTED_FILESYSTEM_MARKER = "[retracted - filesystem check shows no changes landed]"
_RETRACTABLE_STEPS: frozenset[TaskStage] = frozenset({TaskStage.IMPLEMENTING, TaskStage.TESTING, TaskStage.ACCEPTING})
_FILES_CHANGED_PLACEHOLDERS = {"none", "n/a", "-", ""}


def append_activity_entry(workspace: Workspace, task: TaskRecord, entry: TaskActivityEntry) -> None:
    """
    Append one task activity entry through the injected workspace.

    This helper keeps activity-call sites paired with rendering helpers
    while avoiding hidden root-to-workspace conversion inside the task
    layer.
    """
    task_activity_store_for_task(workspace, task).append(entry)


def normalized_files_changed(paths: Iterable[str]) -> list[str]:
    """
    Clean and de-duplicate the agent-reported ``files_changed`` list.

    Agents submit ``"none"``/``"-"``/``""`` placeholders when no files
    changed; left in the activity feed these would pollute the requeue-time
    filesystem-validation check that compares claimed paths against main.
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        stripped = str(raw_path).strip().strip("/")
        if stripped.lower() in _FILES_CHANGED_PLACEHOLDERS or not stripped:
            continue
        if stripped in seen:
            continue
        seen.add(stripped)
        normalized.append(stripped)
    return normalized


def is_retracted_activity_entry(entry: TaskActivityEntry) -> bool:
    """
    Detect entries already marked as retracted.

    Used by the requeue-time check before re-marking, so a second pass does
    not append the marker twice. The string-marker form is intentional: the
    entry shape predates a dedicated retraction flag and migrating every
    historical row would lose the markers.
    """
    return RETRACTED_FILESYSTEM_MARKER in entry.message


def is_retractable_pass_entry(entry: TaskActivityEntry) -> bool:
    """
    Identify pass verdicts that the requeue-time filesystem check may retract.

    Only implement/test/accept stages with claimed file edits qualify: those
    are the verdicts whose validity depends on the worktree actually having
    changes, so a stage with no file claims (e.g. grooming) cannot be falsely
    invalidated by an empty diff.
    """
    return (
        entry.verdict == Verdict.PASS
        and entry.stage in _RETRACTABLE_STEPS
        and bool(normalized_files_changed(entry.files_changed))
    )


def retract_activity_entry(entry: TaskActivityEntry) -> bool:
    """
    Mutate a previously-passed entry in place to record an invalidated pass.

    Returns ``False`` when the entry was already retracted so callers don't
    double-mark or rewrite history; ``True`` after a fresh marker is appended.
    Called from the requeue path when the filesystem check shows no changes
    landed for the stage's claimed files.
    """
    if is_retracted_activity_entry(entry):
        return False
    entry.message = f"{entry.message.rstrip()}\n{RETRACTED_FILESYSTEM_MARKER}"
    return True


def render_task_activity(workspace: Workspace, task: TaskRecord, for_prompt: bool = False) -> str:
    """
    Render the activity feed for operator inspection or prompt context.

    The ``for_prompt`` branch withholds the body of retracted entries so
    subagents are not biased by reports the system has already invalidated;
    operator-facing renders show the full text so a human can audit what was
    retracted and why.
    """
    activity_entries = task_activity_store_for_task(workspace, task).load()
    if not activity_entries:
        return ""
    lines = ["Task activity:"]
    for entry in activity_entries:
        if for_prompt and is_retracted_activity_entry(entry):
            header = (
                f"[{entry.created_at}] {entry.role} ({entry.stage}) - {entry.verdict} {RETRACTED_FILESYSTEM_MARKER}"
            )
            lines.append(f"\n--- {header} ---")
            lines.append("Prior pass report withheld from prompt context after requeue-time filesystem validation.")
            claimed_files = normalized_files_changed(entry.files_changed)
            if claimed_files:
                lines.append(f"Claimed files: {', '.join(claimed_files)}")
            continue
        if entry.verdict_classification:
            verdict_label = f"{entry.verdict}; classification={entry.verdict_classification}"
        else:
            verdict_label = str(entry.verdict)
        header = f"[{entry.created_at}] {entry.role} ({entry.stage}) - {verdict_label}"
        lines.append(f"\n--- {header} ---")
        lines.append(entry.message)
        if entry.files_changed:
            lines.append(f"Files: {', '.join(entry.files_changed)}")
    return "\n".join(lines)
