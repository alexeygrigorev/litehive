"""Workspace-level repair entrypoints."""

from __future__ import annotations

import logging
from pathlib import Path
import shutil
import subprocess

from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.fs_cleanup import remove_tree_logged
from litehive.observability.venv_health import broken_venv_issue_message, probe_broken_venv_executables

from .execution_recovery import recover_stale_runner_state

logger = logging.getLogger(__name__)
_TERMINAL_UNMERGED_WORKTREE_TASK_STATUSES = {
    "abandoned",
    "cancelled",
    "deferred",
    "done",
    "duplicate",
    "wont_do",
}


def repair_workspace_state(root: Path, *, repair_broken_venvs_in_checkouts: bool = False) -> WorkspaceRepairSummary:
    summary = WorkspaceRepairSummary()
    summary.stale_runner_recovered = recover_stale_runner_state(root, summary=summary)
    summary.stale_unmerged_worktrees_removed = _remove_stale_unmerged_worktrees(root)
    summary.mutated = summary.stale_runner_recovered or summary.stale_unmerged_worktrees_removed > 0
    if repair_broken_venvs_in_checkouts:
        repaired_any, remaining = _repair_broken_checkout_venvs(root)
        summary.mutated = summary.mutated or repaired_any
        summary.broken_venv_binaries.extend(remaining)
    return summary


def _remove_stale_unmerged_worktrees(root: Path) -> int:
    from litehive.state.locking import workspace_lock
    from litehive.state.persist import load_state, save_state_without_runner_guard

    with workspace_lock(root):
        state = load_state(root)
        if not state.unmerged_worktrees:
            return 0

        remaining = [entry for entry in state.unmerged_worktrees if not _is_stale_unmerged_worktree(root, entry)]
        removed = len(state.unmerged_worktrees) - len(remaining)
        if removed == 0:
            return 0

        state.unmerged_worktrees = remaining
        save_state_without_runner_guard(root, state)
        return removed


def _is_stale_unmerged_worktree(root: Path, entry) -> bool:
    from litehive.state.records import get_task_record

    worktree_path = _resolve_unmerged_worktree_path(root, entry.worktree_path)
    if not _path_exists(worktree_path):
        return True

    task = get_task_record(root, entry.task_id)
    if task is None:
        return False
    return str(task.status) in _TERMINAL_UNMERGED_WORKTREE_TASK_STATUSES


def _resolve_unmerged_worktree_path(root: Path, worktree_path: str) -> Path:
    path = Path(worktree_path).expanduser()
    if path.is_absolute():
        return path
    return root / path


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _repair_broken_checkout_venvs(root: Path) -> tuple[bool, list[str]]:
    findings = probe_broken_venv_executables(root)
    if not findings:
        return False, []
    if shutil.which("uv") is None:
        return False, [broken_venv_issue_message(root, finding) for finding in findings]

    repaired_any = False
    remaining: list[str] = []
    checkouts: dict[Path, tuple[Path, list[object]]] = {}
    for finding in findings:
        checkout_root = finding.checkout.checkout_root.resolve()
        checkout = checkouts.setdefault(checkout_root, (finding.checkout.venv_path, []))
        checkout[1].append(finding)

    for checkout_root, (venv_path, checkout_findings) in checkouts.items():
        if not (checkout_root / "pyproject.toml").exists():
            remaining.extend(broken_venv_issue_message(root, finding) for finding in checkout_findings)
            continue
        try:
            if venv_path.exists() or venv_path.is_symlink():
                remove_tree_logged(venv_path, logger=logger, target_label="broken checkout venv")
        except OSError as exc:
            remaining.append(f"{checkout_root}: {exc}")
            continue
        sync = subprocess.run(
            ["uv", "sync", "--extra", "dev"],
            cwd=str(checkout_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if sync.returncode != 0:
            remaining.append(
                f"{checkout_root}: {sync.stderr.strip() or sync.stdout.strip() or 'uv sync failed during venv rebuild'}"
            )
            continue
        repaired_any = True

    post_repair_findings = probe_broken_venv_executables(root)
    seen = set(remaining)
    for finding in post_repair_findings:
        message = broken_venv_issue_message(root, finding)
        if message in seen:
            continue
        seen.add(message)
        remaining.append(message)
    return repaired_any, remaining


__all__ = [
    "repair_workspace_state",
]
