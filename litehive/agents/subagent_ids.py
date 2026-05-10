"""SQLite-backed subagent id allocation."""

import re
import sqlite3

from litehive.domain.agent import SubagentId
from litehive.domain.common import utcnow
from litehive.domain.task import TaskRecord
from litehive.workspace import Workspace

_SUBAGENT_ID_RE = re.compile(r"^SA-(\d{4})$")


class SubagentIdRepository:
    """
    Allocate task-scoped ``SA-NNNN`` ids from SQLite.

    The counter table is the source of future allocations. Existing
    task refs and session rows only provide a one-time lower bound so
    upgraded workspaces do not reuse ids that predate the counter row.
    Artifact directories are deliberately ignored because runtime
    state belongs in SQLite, not in folder names.
    """

    def __init__(self, workspace: Workspace) -> None:
        """Store the workspace database handle used for allocations."""
        self.workspace = workspace
        self.counter_store = SubagentCounterStore(workspace)

    def reserve_next_id(self, task: TaskRecord) -> SubagentId:
        """
        Reserve and return the next subagent id for ``task``.

        The read/advance happens in one SQLite transaction so repeated
        manager calls cannot allocate the same id within a task.
        """
        next_number = self.counter_store.reserve_next_number(task)
        return SubagentId(f"SA-{next_number:04d}")


class SubagentCounterStore:
    """
    SQLite-backed task-scoped subagent id counter.
    """

    def __init__(self, workspace: Workspace) -> None:
        """
        Store the workspace database handle used for id allocations.
        """
        self.workspace = workspace

    def reserve_next_number(self, task: TaskRecord) -> int:
        """
        Reserve and return the next numeric suffix for ``task``.
        """
        task_ref_next_number = _next_number_after_task_refs(task)
        with self.workspace.connect() as connection:
            counter_next_number = self._counter_next_number(connection, task.id)
            session_next_number = self._session_next_number(connection, task.id)
            next_number = max(counter_next_number, session_next_number, task_ref_next_number)
            self._save_counter_next_number(connection, task.id, next_number + 1)
        return next_number

    def _counter_next_number(self, connection: sqlite3.Connection, task_id: str) -> int:
        """
        Read the persisted next number, defaulting to the first id.
        """
        row = connection.execute(
            """
            SELECT next_number
            FROM subagent_id_counters
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return 1
        return int(row["next_number"])

    def _session_next_number(self, connection: sqlite3.Connection, task_id: str) -> int:
        """
        Derive the lower bound from persisted subagent session rows.
        """
        rows = connection.execute(
            """
            SELECT subagent_id
            FROM subagent_sessions
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchall()
        next_number = 1
        for row in rows:
            next_number = max(next_number, _next_number_after_id(str(row["subagent_id"])))
        return next_number

    def _save_counter_next_number(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        next_number: int,
    ) -> None:
        """
        Persist the next number that should be allocated after this call.
        """
        connection.execute(
            """
            INSERT INTO subagent_id_counters (task_id, next_number, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                next_number = excluded.next_number,
                updated_at = excluded.updated_at
            """,
            (task_id, next_number, utcnow()),
        )


def _next_number_after_task_refs(task: TaskRecord) -> int:
    """
    Return the next number after ids already persisted on the task.
    """
    next_number = 1
    for ref in task.subagents:
        next_number = max(next_number, _next_number_after_id(ref.id))
    return next_number


def _next_number_after_id(subagent_id: str) -> int:
    """
    Return one greater than a canonical ``SA-NNNN`` id, or ``1``.
    """
    match = _SUBAGENT_ID_RE.match(subagent_id)
    if match is None:
        return 1
    return int(match.group(1)) + 1
