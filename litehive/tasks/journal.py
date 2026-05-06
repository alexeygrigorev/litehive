"""Task journal helpers backed by SQLite."""

from dataclasses import dataclass
import json

from litehive.domain.task import TaskRecord
from litehive.state.store import runtime_store_for_workspace
from litehive.workspace import Workspace


@dataclass(frozen=True, slots=True)
class TaskJournalEntry:
    """
    One row of the per-task journal.

    The operator-readable narrative captured at every lifecycle transition,
    surfaced by ``litehive task logs`` and folded into the recovery agent's
    evidence builder so a triage prompt sees the same story the operator does.
    """

    task_id: str
    entry_index: int
    created_at: str
    message: str
    metadata: dict[str, object]


def append_journal(workspace: Workspace, task: TaskRecord, message: str) -> None:
    """
    Append one operator-readable line to the task journal.

    The only sanctioned write path: every lifecycle transition reaches the
    same store via this helper so the SQLite-side and event-log-side journals
    stay aligned.
    """
    runtime_store_for_workspace(workspace).append_task_journal(task.id, message)


def load_task_journal(workspace: Workspace, task_id: str) -> list[TaskJournalEntry]:
    """
    Read the journal entries for a task in chronological order.

    Consumed by ``litehive task logs`` and the recovery agent's evidence
    builder; the chronological ordering is by ``entry_index`` rather than
    timestamp so concurrent inserts from one transaction stay stable.
    """
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
    """
    Render the journal as a Markdown document for ``litehive task logs``.

    Returns the empty string when the task has no entries so callers can
    skip the section without a guard; a header-only render would be misread
    as "task exists but has no journal yet".
    """
    entries = load_task_journal(workspace, task.id)
    if not entries:
        return ""
    lines = [f"# {task.id} {task.title}"]
    for entry in entries:
        lines.append("")
        lines.append(f"## {entry.created_at}")
        lines.append(entry.message)
    return "\n".join(lines).rstrip() + "\n"
