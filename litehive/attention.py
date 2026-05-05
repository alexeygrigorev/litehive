"""Operator-needed status projection backed by SQLite.

Litehive no longer persists a rich attention-item queue. Operator
visibility is derived from authoritative task and runner state:
flagged tasks and pool stop reasons that require human action.

Best-effort operator diagnostics that don't fit those structured
sources (e.g. "merge-resolver wrapper rejected a destructive git
command") are appended to the ``attention_log`` table instead of
the previous file-based runtime log. SQLite is the source of
truth for everything else in the workspace; the attention log
follows.
"""

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from litehive.config.workspace import normalize_workspace_root
from litehive.db.schema import connect_workspace_db
from litehive.domain.common import TaskStatus, utcnow
from litehive.domain.task import TaskRecord
from litehive.state.persist import load_state
from litehive.state.records import list_tasks

OPERATOR_NEEDED_POOL_STOP_REASONS = {
    "attention_required",
    "consecutive_task_failures",
    "continue_or_rollback_required",
    "dirty_git_state",
    "diverged_from_origin",
    "human_checkpoint_before_acceptance",
    "human_checkpoint_before_commit",
    "human_checkpoint_reached",
}


@dataclass(frozen=True, slots=True)
class OperatorNeededState:
    flagged_tasks: tuple[TaskRecord, ...]
    pool_stop_reason: str | None

    @property
    def needed(self) -> bool:
        return bool(self.flagged_tasks) or self.pool_stop_reason is not None


@dataclass(frozen=True, slots=True)
class AttentionLogEntry:
    created_at: str
    message: str


def append_attention_log(workspace: Path, message: str) -> None:
    """Persist a best-effort operator diagnostic to the attention log table.

    Used by the merge-resolver git wrapper, the daemon's
    origin-divergence guard, and any other code path that needs to
    record a one-off operator-facing event that doesn't fit
    elsewhere. Schema lives at migration 0009.
    """
    root = normalize_workspace_root(workspace, source="append_attention_log")
    with connect_workspace_db(root) as connection:
        connection.execute(
            "INSERT INTO attention_log (created_at, message) VALUES (?, ?)",
            (utcnow(), message),
        )
        connection.commit()


def read_attention_log(workspace: Path, limit: int | None = None) -> list[AttentionLogEntry]:
    """Return attention-log entries newest-first, optionally limited.

    Reading is best-effort: if the table does not yet exist (e.g.
    migrations have not run on a freshly-initialized workspace),
    return an empty list rather than raising.
    """
    root = normalize_workspace_root(workspace, source="read_attention_log")
    query = "SELECT created_at, message FROM attention_log ORDER BY id DESC"
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    with connect_workspace_db(root) as connection:
        try:
            rows = connection.execute(query).fetchall()
        except sqlite3.OperationalError:
            return []
    return [AttentionLogEntry(created_at=row[0], message=row[1]) for row in rows]


def collect_operator_needed_state(root: Path) -> OperatorNeededState:
    root = normalize_workspace_root(root, source="collect_operator_needed_state")
    state = load_state(root, bootstrap=False)
    flagged_tasks = tuple(
        sorted(
            (task for task in list_tasks(root, strict=False) if task.status == TaskStatus.FLAGGED),
            key=lambda task: task.id,
        )
    )
    pool_stop_reason = state.pool_stop_reason
    if pool_stop_reason not in OPERATOR_NEEDED_POOL_STOP_REASONS:
        pool_stop_reason = None
    return OperatorNeededState(flagged_tasks=flagged_tasks, pool_stop_reason=pool_stop_reason)


def waiting_for_you_lines(root: Path, limit: int = 5, reconcile: bool = True) -> list[str]:
    del reconcile
    try:
        state = collect_operator_needed_state(root)
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        return [f"operator_needed: unavailable ({type(exc).__name__}: {exc})"]

    lines = [f"operator_needed: {str(state.needed).lower()}"]
    if state.pool_stop_reason is not None:
        lines.append(f"operator_needed_pool_stop_reason: {state.pool_stop_reason}")
    lines.append(f"operator_needed_tasks: {len(state.flagged_tasks)}")
    for task in state.flagged_tasks[:limit]:
        reason = task.flag_reason or "unknown"
        stage = task.pipeline_status or "-"
        lines.append(f"operator_needed_task: {task.id} stage={stage} reason={reason}")
    return lines
