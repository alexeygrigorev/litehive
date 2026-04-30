"""Minimal operator-needed status projection.

Litehive no longer persists a rich attention-item queue. Operator visibility is
derived from authoritative task and runner state: flagged tasks and pool stop
reasons that require human action.
"""

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from litehive.config.workspace import normalize_workspace_root
from litehive.config.workspace_files import workspace_dir
from litehive.domain.common import utcnow
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


def append_attention_log(workspace: Path, message: str) -> None:
    """Append a best-effort operator diagnostic to the runtime log."""
    root = normalize_workspace_root(workspace, source="append_attention_log")
    path = workspace_dir(root) / "runtime" / "attention.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utcnow()}\t{message}\n")


def collect_operator_needed_state(root: Path) -> OperatorNeededState:
    root = normalize_workspace_root(root, source="collect_operator_needed_state")
    state = load_state(root, bootstrap=False)
    flagged_tasks = tuple(
        sorted(
            (task for task in list_tasks(root, strict=False) if task.status == "flagged"),
            key=lambda task: task.id,
        )
    )
    pool_stop_reason = state.pool_stop_reason
    if pool_stop_reason not in OPERATOR_NEEDED_POOL_STOP_REASONS:
        pool_stop_reason = None
    return OperatorNeededState(flagged_tasks=flagged_tasks, pool_stop_reason=pool_stop_reason)


def waiting_for_you_lines(root: Path, *, limit: int = 5, reconcile: bool = True) -> list[str]:
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
