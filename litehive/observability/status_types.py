"""Shared types and constants for the `litehive status`/`litehive health` diagnostics."""

from dataclasses import dataclass
from typing import Literal

from litehive.config.model import LitehiveConfig
from litehive.domain.common import TaskStage
from litehive.domain.engine import WorkspaceEngineMonitoring
from litehive.domain.runtime import RunnerStatusState
from litehive.domain.task import WorkspaceState

_STATUS_WEDGED_HEARTBEAT_SECONDS = 10 * 60
_RECOVERY_FAILURE_FLAG_REASONS = {
    "crash_budget_exhausted",
    "recovery_budget_exhausted",
    "recovery_failed",
}
_RECOVERY_FAILURE_STATE_REASONS = {
    "pre_exec_recovery_failed",
    "recovery_budget_hit",
    "recovery_crashed",
    "recovery_exhausted",
    "recovery_missing_target_stage",
}
_RESUMABLE_PIPELINE_STAGES: frozenset[TaskStage] = frozenset(
    {TaskStage.GROOMING, TaskStage.IMPLEMENTING, TaskStage.TESTING, TaskStage.ACCEPTING, TaskStage.COMMIT_TO_GIT}
)
_TRUSTED_STAGE_MARKER_STATUSES = {"idle", "paused", "interrupted", "running"}
_TASKS_UNAVAILABLE_KEYS = {"state"}

StatusSeverity = Literal["WARN", "ERROR"]


@dataclass(slots=True)
class StatusIssue:
    key: str
    severity: StatusSeverity
    message: str

    def render(self) -> str:
        return f"{self.key}: {self.message}"


@dataclass(slots=True)
class StatusSnapshot:
    config: LitehiveConfig
    state: WorkspaceState
    runner: RunnerStatusState
    monitoring: WorkspaceEngineMonitoring
    issues: list[StatusIssue]


@dataclass(slots=True)
class _RecoveryFailureContext:
    failed_reason: str | None = None
    explanation: str | None = None
    origin_stage: str | None = None
