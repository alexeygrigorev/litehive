"""
Shared types and constants for status diagnostics.

Owns the typed shapes (:class:`StatusIssue`,
:class:`StatusSnapshot`, recovery context) and constant sets
(failure-reason allowlists, resumable stages, wedged threshold)
that the loaders, probes, and renderers share. Living in one
place prevents the constants from drifting across the diagnostic
modules.
"""

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
    """
    A single diagnostic finding surfaced by ``litehive status`` or ``health``.

    Each issue carries a machine-readable key, a severity level, and a
    human-readable message with a remediation tail (separated by `` — ``).
    Probes produce lists of these; renderers format them into the text
    blocks the CLI prints.

    ``key`` identifies the issue category (e.g. ``runner_state``,
    ``config``) so monitoring scripts can grep for specific fault families.
    ``severity`` is ``"WARN"`` for degraded info or ``"ERROR"`` for a
    broken workspace.
    ``message`` is the human-readable diagnosis followed by
    `` — <remediation>``.
    """

    key: str
    severity: StatusSeverity
    message: str

    def render(self) -> str:
        """
        Format an issue as a single ``key: message`` line.

        Used by the verbose status diagnostics block.
        Concentrating the wire format here so every consumer
        renders issues identically — without the helper, a CLI
        path and a daemon path could format differently and
        break monitoring scripts that grep on the exact shape.
        """
        return f"{self.key}: {self.message}"


@dataclass(slots=True)
class StatusSnapshot:
    """
    Immutable aggregate of every loader result and probe finding.

    The diagnostics pipeline builds one snapshot per ``status`` /
    ``health`` invocation and passes it to the renderer. ``config``,
    ``state``, and ``runner`` come from the loaders; ``monitoring``
    carries engine usage stats; ``issues`` collects every probe's
    findings.
    """

    config: LitehiveConfig
    state: WorkspaceState
    runner: RunnerStatusState
    monitoring: WorkspaceEngineMonitoring
    issues: list[StatusIssue]


@dataclass(slots=True)
class _RecoveryFailureContext:
    """
    Recovery-failure details pulled from lifecycle persistence.

    ``failed_reason`` is the structured reason the scheduler recorded
    (e.g. ``recovery_budget_exhausted``). ``explanation`` is the
    human-readable text the recovery agent or operator can act on.
    ``origin_stage`` is the pipeline stage where the original failure
    happened, used to point the operator at the right place in the
    pipeline.
    """

    failed_reason: str | None = None
    explanation: str | None = None
    origin_stage: str | None = None
