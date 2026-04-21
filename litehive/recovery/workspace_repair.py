"""Workspace-level repair entrypoints."""

from pathlib import Path

from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.observability.venv_health import probe_broken_venv_executables
from .execution_recovery import (
    interruption_journal_message,
    is_stranded_commit_task,
    mark_interrupted_subagent,
    prepare_interrupted_task,
    recover_stale_runner_state,
    should_requeue_commit_stage_task,
    stale_interruption_reason,
)


def repair_workspace_state(root: Path, *, repair_broken_venvs_in_checkouts: bool = False) -> WorkspaceRepairSummary:
    summary = WorkspaceRepairSummary()
    if repair_broken_venvs_in_checkouts:
        # Broken venv entrypoints are reported and block the daemon, but `repair`
        # does not auto-rebuild `.venv`; it only reports the exact remediation target.
        summary.broken_venv_binaries = [
            f"{finding.checkout.venv_path}:{finding.binary_name}" for finding in probe_broken_venv_executables(root)
        ]
    summary.stale_runner_recovered = recover_stale_runner_state(root, summary=summary)
    summary.mutated = summary.stale_runner_recovered
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
