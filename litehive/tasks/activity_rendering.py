"""Task activity rendering and retraction helpers."""

from pathlib import Path
from typing import Iterable

from litehive.domain.reports import TaskActivityEntry
from litehive.domain.task import TaskRecord
from litehive.tasks.activity import append_task_activity, load_task_activity

RETRACTED_FILESYSTEM_MARKER = "[retracted - filesystem check shows no changes landed]"
_RETRACTABLE_STEPS = {"implementing", "testing", "accepting"}
_FILES_CHANGED_PLACEHOLDERS = {"none", "n/a", "-", ""}


def append_activity_entry(root: Path, task: TaskRecord, entry: TaskActivityEntry) -> None:
    append_task_activity(root, task, entry)


def normalized_files_changed(paths: Iterable[str]) -> list[str]:
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
    return RETRACTED_FILESYSTEM_MARKER in entry.message


def is_retractable_pass_entry(entry: TaskActivityEntry) -> bool:
    return (
        entry.verdict == "pass"
        and entry.stage in _RETRACTABLE_STEPS
        and bool(normalized_files_changed(entry.files_changed))
    )


def retract_activity_entry(entry: TaskActivityEntry) -> bool:
    if is_retracted_activity_entry(entry):
        return False
    entry.message = f"{entry.message.rstrip()}\n{RETRACTED_FILESYSTEM_MARKER}"
    return True


def render_task_activity(root: Path, task: TaskRecord, *, for_prompt: bool = False) -> str:
    activity_entries = load_task_activity(root, task)
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
        verdict_label = (
            f"{entry.verdict}; classification={entry.verdict_classification}"
            if entry.verdict_classification
            else str(entry.verdict)
        )
        header = f"[{entry.created_at}] {entry.role} ({entry.stage}) - {verdict_label}"
        lines.append(f"\n--- {header} ---")
        lines.append(entry.message)
        if entry.files_changed:
            lines.append(f"Files: {', '.join(entry.files_changed)}")
    return "\n".join(lines)
