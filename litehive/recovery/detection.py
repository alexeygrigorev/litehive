"""Small recovery exceptions shared by task selection."""

from __future__ import annotations

from typing import Literal

LaunchFailureContext = Literal[
    "cycle_start_failed",
    "worktree_setup_failed",
    "venv_sync_failed",
    "pre_stage_setup_failed",
]

LaunchDiagnostics = dict[str, str | int | bool | None | list[str]]


class TaskLaunchFailure(RuntimeError):
    """Raised when a task cannot be selected or prepared for execution."""

    def __init__(
        self,
        *,
        context: LaunchFailureContext,
        summary: str,
        diagnostics: LaunchDiagnostics | None = None,
    ) -> None:
        super().__init__(summary)
        self.context = context
        self.summary = summary
        self.diagnostics = dict(diagnostics or {})
