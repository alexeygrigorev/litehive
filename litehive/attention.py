"""
Operator-attention projection plus the SQLite-backed attention log.

Two related concerns live here. First, ``collect_operator_needed_state``
projects "is operator action required?" from authoritative task and
runner state — flagged tasks plus pool stop reasons that require human
intervention. There's no separate attention-item queue; operator
visibility is derived directly from the source records so the two
can't drift.

Second, ``attention_log`` is a free-form fallback for diagnostics that
don't fit the structured sources (the merge-resolver wrapper
rejecting a destructive git command, the daemon noticing
origin-divergence). Persisting these in SQLite (migration 0009) keeps
the project-wide rule "everything in SQLite" intact instead of
maintaining a parallel file-based runtime log.
"""

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from litehive.config.workspace import normalize_workspace_root
from litehive.domain.common import TaskStatus, utcnow
from litehive.domain.task import TaskRecord
from litehive.state.persist import load_state_for_workspace
from litehive.workspace import Workspace

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
    """
    "Is operator action required?" snapshot rendered by status surfaces.

    Produced by :func:`collect_operator_needed_state` from
    authoritative SQLite state — no separate persistence, so the
    snapshot can never disagree with the underlying task and pool
    state. Frozen and slotted so it's cheap to share across
    rendering passes.
    """

    flagged_tasks: tuple[TaskRecord, ...]
    pool_stop_reason: str | None

    @property
    def needed(self) -> bool:
        """
        True when the daemon must wait for an operator.

        Triggered by any flagged task or by a pool stop reason in
        ``OPERATOR_NEEDED_POOL_STOP_REASONS`` — those are the
        reasons that require human action rather than auto-clearing
        on the next iteration.
        """
        return bool(self.flagged_tasks) or self.pool_stop_reason is not None


@dataclass(frozen=True, slots=True)
class AttentionLogEntry:
    """
    One row of the ``attention_log`` table.

    Free-form operator diagnostic that doesn't fit the structured
    task/runner state — the merge-resolver git wrapper recording
    "blocked destructive command", the daemon flagging a
    transient backup failure, etc. Rendered chronologically by
    status surfaces and the operator timeline.
    """

    created_at: str
    message: str


def append_attention_log(workspace: Workspace, message: str) -> None:
    """
    Persist a best-effort operator diagnostic to the attention-log table.

    Used by the merge-resolver git wrapper, the daemon's
    origin-divergence guard, and any other code path that needs
    to record a one-off operator-facing event that doesn't fit
    the structured task/runner records. Schema lives at migration
    0009; entries are append-only so the timeline stays
    chronological.
    """
    with workspace.connect() as connection:
        connection.execute(
            "INSERT INTO attention_log (created_at, message) VALUES (?, ?)",
            (utcnow(), message),
        )
        connection.commit()


def read_attention_log(workspace: Workspace, limit: int | None = None) -> list[AttentionLogEntry]:
    """
    Return attention-log entries newest-first, optionally limited.

    Reads are best-effort: a freshly-initialized workspace where
    migrations haven't yet run returns ``[]`` instead of raising,
    so the status path can render a sensible "no attention items"
    block before the first daemon tick. Pre-migration crashes
    also fall through cleanly.
    """
    query = "SELECT created_at, message FROM attention_log ORDER BY id DESC"
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    with workspace.connect() as connection:
        try:
            rows = connection.execute(query).fetchall()
        except sqlite3.OperationalError:
            return []
    return [AttentionLogEntry(created_at=row[0], message=row[1]) for row in rows]


def collect_operator_needed_state(root: Path) -> OperatorNeededState:
    """
    Path-based compatibility wrapper for operator-attention projection.
    """
    return collect_operator_needed_state_for_workspace(
        Workspace.from_path(normalize_workspace_root(root, source="collect_operator_needed_state"))
    )


def collect_operator_needed_state_for_workspace(workspace: Workspace) -> OperatorNeededState:
    """
    Project the current attention requirement from authoritative SQLite state.

    Reads flagged tasks and the pool stop reason and filters the
    latter to the operator-needed allow list, so transient stop
    reasons like ``queue_exhausted`` don't show up as attention
    items. Called by :func:`waiting_for_you_lines` for status
    rendering and by the daemon's pool gate before deciding
    whether to iterate.
    """
    state = load_state_for_workspace(workspace, bootstrap=False)
    flagged_tasks = tuple(
        sorted(
            (task for task in workspace.list_tasks(strict=False) if task.status == TaskStatus.FLAGGED),
            key=lambda task: task.id,
        )
    )
    pool_stop_reason = state.pool_stop_reason
    if pool_stop_reason not in OPERATOR_NEEDED_POOL_STOP_REASONS:
        pool_stop_reason = None
    return OperatorNeededState(flagged_tasks=flagged_tasks, pool_stop_reason=pool_stop_reason)


def waiting_for_you_lines(root: Path, limit: int = 5, reconcile: bool = True) -> list[str]:
    """
    Path-based compatibility wrapper for the "waiting on you" status block.
    """
    return waiting_for_you_lines_for_workspace(
        Workspace.from_path(normalize_workspace_root(root, source="waiting_for_you_lines")),
        limit=limit,
        reconcile=reconcile,
    )


def waiting_for_you_lines_for_workspace(workspace: Workspace, limit: int = 5, reconcile: bool = True) -> list[str]:
    """
    Render the "waiting on you" status block.

    Consumed by ``litehive status`` and the operator dashboard.
    Degrades to a single error line on database failure rather
    than crashing — status is the operator's first stop when
    something is wrong, and crashing here would obscure whatever
    they were trying to diagnose.
    """
    del reconcile
    try:
        state = collect_operator_needed_state_for_workspace(workspace)
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
