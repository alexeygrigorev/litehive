"""Task journal helpers backed by SQLite."""

from dataclasses import dataclass
import json

from litehive.domain.task import TaskRecord
from litehive.state.store import runtime_store
from litehive.workspace import Workspace


@dataclass(frozen=True, slots=True)
class TaskJournalEntry:
    """One row of the per-task journal: the operator-readable narrative captured at lifecycle transitions and surfaced by `task logs`."""

    task_id: str
    entry_index: int
    created_at: str
    message: str
    metadata: dict[str, object]


def append_journal(workspace: Workspace, task: TaskRecord, message: str) -> None:
    """Append one operator-readable line to the task journal; the only sanctioned write path so every lifecycle transition reaches the same store."""
    runtime_store(workspace.root).append_task_journal(task.id, message)


def load_task_journal(workspace: Workspace, task_id: str) -> list[TaskJournalEntry]:
    """Read the journal entries for a task in chronological order; consumed by `task logs` and the recovery agent's evidence builder."""
    with workspace.connect() as connection:
        rows = connection.execute(
            """
            SELECT task_id, entry_index, created_at, message, metadata
            FROM task_journal
            WHERE task_id = ?
            ORDER BY entry_index ASC
            """,
            (task_id,),
        ).fetchall()
    entries: list[TaskJournalEntry] = []
    for row in rows:
        try:
            metadata = json.loads(str(row["metadata"]))
        except json.JSONDecodeError:
            metadata = {}
        if isinstance(metadata, dict):
            metadata_value = metadata
        else:
            metadata_value = {}
        entries.append(
            TaskJournalEntry(
                task_id=str(row["task_id"]),
                entry_index=int(row["entry_index"]),
                created_at=str(row["created_at"]),
                message=str(row["message"]),
                metadata=metadata_value,
            )
        )
    return entries


def render_task_journal(workspace: Workspace, task: TaskRecord) -> str:
    """Render the journal as a readable Markdown document for `task logs`; returns the empty string when the task has no entries so callers can skip the section without a guard."""
    entries = load_task_journal(workspace, task.id)
    if not entries:
        return ""
    lines = [f"# {task.id} {task.title}"]
    for entry in entries:
        lines.append("")
        lines.append(f"## {entry.created_at}")
        lines.append(entry.message)
    return "\n".join(lines).rstrip() + "\n"
