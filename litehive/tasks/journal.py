"""Task journal append helper."""

from pathlib import Path

from litehive.models.common import utcnow
from litehive.models.task_models import TaskRecord

from litehive.state.locking import workspace_mutation_guard
from .paths import task_dir
from .persistence import atomic_write_text


def append_journal(root: Path, task: TaskRecord, message: str) -> None:
    journal = task_dir(root, task) / "journal.md"
    with workspace_mutation_guard(root):
        existing = journal.read_text(encoding="utf-8") if journal.exists() else ""
        atomic_write_text(journal, f"{existing}\n## {utcnow()}\n{message}\n")
