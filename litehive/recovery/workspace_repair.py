"""Workspace repair entrypoint used by the daemon and repair CLI."""

from __future__ import annotations

from pathlib import Path

from litehive.domain.task_ops import WorkspaceRepairSummary

from .execution_recovery import recover_stale_runner_state


def repair_workspace_state(root: Path) -> WorkspaceRepairSummary:
    summary = WorkspaceRepairSummary()
    summary.stale_runner_recovered = recover_stale_runner_state(root, summary=summary)
    summary.mutated = summary.stale_runner_recovered
    return summary


__all__ = [
    "repair_workspace_state",
]
