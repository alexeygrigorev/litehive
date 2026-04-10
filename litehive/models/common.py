"""Shared literals and helpers for litehive models.

The engine-facing literals, helpers, and constants (SubagentStatus,
EngineMonitoringSource, EngineLimitKind, LiveEventKind, LiveEventRole,
OutcomeKind, OutcomeReasonCode, RetrySource, cap_feedback, utcnow,
FEEDBACK_CAP, and the task/effort/checkpoint literals that flow through
agent submissions) now live in `heru.types`. This module re-exports them
for backward compatibility and keeps litehive-only types (PipelineMode,
TaskStatus, PipelineStatus, etc.) authoritative here.
"""

from typing import Literal

from heru.types import (
    FEEDBACK_CAP,
    EngineLimitKind,
    EngineMonitoringSource,
    HumanCheckpoint,
    LiveEventKind,
    LiveEventRole,
    OutcomeKind,
    OutcomeReasonCode,
    PlannedEffort,
    RetrySource,
    SubagentStatus,
    TaskComplexity,
    TaskMode,
    _TRUNCATION_MARKER,
    cap_feedback,
    utcnow,
)


PipelineMode = Literal["single", "full"]
TaskStatus = Literal[
    "queued",
    "in_progress",
    "interrupted",
    "parked",
    "done",
    "flagged",
    "merge_failed",
    "cancelled",
    "wont_do",
    "deferred",
    "duplicate",
]
PipelineStatus = Literal[
    "backlog",
    "grooming",
    "implementing",
    "testing",
    "accepting",
    "commit_to_git",
    "done",
    "merge_failed",
]
RunnerExecutionStatus = Literal["idle", "running", "late", "stale"]
UpstreamContributionKind = Literal[
    "runtime_bug",
    "missing_feature",
    "config_improvement",
    "prompt_improvement",
    "engine_adapter_fix",
]


__all__ = [
    "EngineLimitKind",
    "EngineMonitoringSource",
    "FEEDBACK_CAP",
    "HumanCheckpoint",
    "LiveEventKind",
    "LiveEventRole",
    "OutcomeKind",
    "OutcomeReasonCode",
    "PipelineMode",
    "PipelineStatus",
    "PlannedEffort",
    "RetrySource",
    "RunnerExecutionStatus",
    "SubagentStatus",
    "TaskComplexity",
    "TaskMode",
    "TaskStatus",
    "UpstreamContributionKind",
    "cap_feedback",
    "utcnow",
]
