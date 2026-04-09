"""Backward-compatible shim for pipeline core."""

from litehive.pipeline.core import (
    RunResult,
    StageExecutor,
    TaskExecutionRunner,
    _human_checkpoint_reason,
    _reason_code_for_report,
    _record_unmerged_worktree,
)

__all__ = [
    "RunResult",
    "StageExecutor",
    "TaskExecutionRunner",
    "_human_checkpoint_reason",
    "_reason_code_for_report",
    "_record_unmerged_worktree",
]
