"""Workspace-level repair entrypoints."""

from pathlib import Path

import yaml

from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.observability.venv_health import probe_broken_venv_executables
from litehive.state.persist import load_state, save_state_without_runner_guard
from litehive.state.store import runtime_store
from litehive.tasks.activity import migrate_legacy_task_activity_files
from litehive.tasks.archive import archive_root
from litehive.tasks.paths import tasks_root
from litehive.worktree import resolve_recorded_worktree_path
from .execution_recovery import (
    interruption_journal_message,
    is_stranded_commit_task,
    mark_interrupted_subagent,
    prepare_interrupted_task,
    recover_stale_runner_state,
    should_requeue_commit_stage_task,
    stale_interruption_reason,
)

_TERMINAL_UNMERGED_WORKTREE_TASK_STATUSES = frozenset(
    {"done", "archived", "abandoned", "cancelled", "wont_do", "duplicate", "deferred"}
)


def _task_status_from_disk(root: Path, task_id: str) -> str | None:
    task_state = runtime_store(root).load_task_state(task_id)
    if task_state is not None:
        return str(task_state.status)

    archive = archive_root(root)
    for task_yaml in sorted(archive.glob(f"{task_id}-*/task.yaml")):
        try:
            payload = yaml.safe_load(task_yaml.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(payload, dict):
            status = payload.get("status")
            if isinstance(status, str):
                return status
            return "archived"

    for task_yaml in sorted(tasks_root(root, bootstrap=False).glob(f"{task_id}-*/task.yaml")):
        try:
            payload = yaml.safe_load(task_yaml.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(payload, dict):
            status = payload.get("status")
            if isinstance(status, str):
                return status
    return None


def _cleanup_stale_unmerged_worktrees(root: Path, *, summary: WorkspaceRepairSummary) -> bool:
    state = load_state(root)
    if not state.unmerged_worktrees:
        return False

    retained = []
    removed = 0
    for entry in state.unmerged_worktrees:
        worktree_path = resolve_recorded_worktree_path(root, entry.worktree_path)
        if worktree_path is None or not worktree_path.exists():
            removed += 1
            continue
        task_status = _task_status_from_disk(root, entry.task_id)
        if task_status in _TERMINAL_UNMERGED_WORKTREE_TASK_STATUSES:
            removed += 1
            continue
        retained.append(entry)

    if removed == 0:
        return False

    state.unmerged_worktrees = retained
    save_state_without_runner_guard(root, state)
    summary.stale_unmerged_worktrees_removed += removed
    return True


def repair_workspace_state(root: Path, *, repair_broken_venvs_in_checkouts: bool = False) -> WorkspaceRepairSummary:
    summary = WorkspaceRepairSummary()
    if repair_broken_venvs_in_checkouts:
        # Broken venv entrypoints are reported and block the daemon, but `repair`
        # does not auto-rebuild `.venv`; it only reports the exact remediation target.
        summary.broken_venv_binaries = [
            f"{finding.checkout.venv_path}:{finding.binary_name}" for finding in probe_broken_venv_executables(root)
        ]
    migrated_comments = migrate_legacy_task_activity_files(root)
    stale_unmerged_worktrees_removed = _cleanup_stale_unmerged_worktrees(root, summary=summary)
    summary.stale_runner_recovered = recover_stale_runner_state(root, summary=summary)
    summary.mutated = bool(migrated_comments or stale_unmerged_worktrees_removed or summary.stale_runner_recovered)
    return summary


__all__ = [
    "interruption_journal_message",
    "is_stranded_commit_task",
    "mark_interrupted_subagent",
    "prepare_interrupted_task",
    "recover_stale_runner_state",
    "repair_workspace_state",
    "should_requeue_commit_stage_task",
    "stale_interruption_reason",
]
