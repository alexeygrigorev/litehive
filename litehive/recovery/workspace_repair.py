"""Workspace-level repair entrypoints."""

from pathlib import Path

from litehive.domain.task_ops import WorkspaceRepairSummary

from .execution_recovery import recover_stale_runner_state


def repair_workspace_state(root: Path, *, repair_broken_venvs_in_checkouts: bool = False) -> WorkspaceRepairSummary:
    del repair_broken_venvs_in_checkouts
    summary = WorkspaceRepairSummary()
    summary.stale_runner_recovered = recover_stale_runner_state(root, summary=summary)
    summary.mutated = summary.stale_runner_recovered
    return summary


__all__ = [
    "repair_workspace_state",
]
