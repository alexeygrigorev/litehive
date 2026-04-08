"""Task journal append helper."""

from pathlib import Path

from litehive.models import TaskRecord, utcnow

from .locking import workspace_mutation_guard
from .paths import task_dir
from .persistence import _atomic_write_text


def append_journal(root: Path, task: TaskRecord, message: str) -> None:
    journal = task_dir(root, task) / "journal.md"
    with workspace_mutation_guard(root):
        existing = journal.read_text(encoding="utf-8") if journal.exists() else ""
        _atomic_write_text(journal, f"{existing}\n## {utcnow()}\n{message}\n")

