"""Stage, recovery, and reporting models (litehive-native)."""

from typing import Literal, TypeAlias

from pydantic import BaseModel, Field, model_validator

from .common import (
    FEEDBACK_CAP,
    OutcomeKind,
    OutcomeReasonCode,
    TaskStage,
    TRUNCATION_MARKER,
    Verdict,
    cap_feedback,
    utcnow,
)
from .recovery import TriggerEventKind
from .runtime import ResourceLimitEvent


ReportStage: TypeAlias = TaskStage | Literal["merge_resolving", "recovering"]


class StageReport(BaseModel):
    """Normalized machine-readable summary of a pipeline stage execution.

    Separate from ActivityEntry to serve different purposes:
    - ActivityEntry: append-only conversation and review history for humans
    - StageReport: normalized machine-readable summary for routing, reporting,
      and later analysis by PipelineRunner and recovery logic

    Historically heru parsed a `STAGE_RESULT: <yaml>` block out of agent
    stdout to build one of these. That path is gone — agents now submit
    verdicts via the `litehive report` CLI and `stage_report_from_subagent`
    constructs this record directly.

    Primary consumers: PipelineRunner for routing decisions, recovery logic
    for failure analysis, and reporting systems for metrics and debugging.
    """

    task_id: str                                        # Task this report belongs to
    stage: ReportStage                                  # Pipeline stage that was executed
    verdict: Verdict                                    # Final stage verdict (accept, reject, blocked)
    source: Literal["agent", "hook"] = "agent"          # What generated this report
    summary: str                                        # Brief description of stage results
    feedback: str = ""                                  # Detailed feedback or explanation
    submitted_via_cli: bool = False                     # Whether submitted via CLI vs internal
    files_changed: list[str] = Field(default_factory=list)  # Files modified during stage
    created_follow_up_task_ids: list[str] = Field(default_factory=list)  # Follow-up tasks created
    tests: dict[str, int] = Field(default_factory=lambda: {"added": 0, "passing": 0})  # Test metrics
    warnings: list[str] = Field(default_factory=list)  # Non-fatal warnings
    retry_count: int = 0                               # Current retry attempt number
    retry_limit: int = 0                               # Maximum retries allowed
    retry_decision: Literal["continue", "retry", "final"] = "continue"  # Retry routing decision
    outcome: OutcomeKind | None = None                 # Terminal outcome if stage completed task
    outcome_reason_code: OutcomeReasonCode | None = None  # Machine-readable outcome reason
    outcome_reason: str = ""                           # Human-readable outcome explanation
    failure_classification: str | None = None          # Type of failure if applicable
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)  # Detailed failure context
    resource_limit_event: ResourceLimitEvent | None = None  # Resource limit hit during stage
    duration_seconds: int = 0                          # How long stage execution took
    created_at: str = Field(default_factory=utcnow)   # When report was generated


class FollowUpTaskSpec(BaseModel):
    """Specification for creating a follow-up task.

    Used when a stage execution determines that additional work is needed
    that should be tracked as a separate task. Captures the intent and
    requirements for the follow-up task creation.
    """
    title: str                                           # Brief title for the follow-up task
    rationale: str                                       # Why this follow-up task is needed
    blocking: bool = False                               # Whether this blocks the current task
    goal: str = ""                                       # Main objective of the follow-up task
    acceptance_criteria: list[str] = Field(default_factory=list)  # Completion conditions
    task_type: str | None = None                         # Optional task type classification


class RecoveryEvidenceItem(BaseModel):
    """Evidence collected during recovery diagnosis.

    Represents a piece of information gathered by the recovery agent
    to understand the failure context and determine appropriate recovery
    actions. Used to document what was checked during recovery.
    """
    kind: str                   # Type of evidence (file, log, state, etc.)
    label: str                  # Human-readable description
    path: str | None = None     # Filesystem path if applicable
    exists: bool = False        # Whether the evidence was found/valid
    summary: str = ""           # Brief summary of findings
    metadata: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)  # Additional context


class RecoveryAction(BaseModel):
    """Action taken during recovery to address a failure.

    Documents what the recovery agent did to attempt to resolve the
    issue that triggered recovery. Used for recovery audit trails
    and understanding recovery effectiveness.
    """
    action: str                 # Description of the recovery action taken
    applied: bool = True        # Whether the action was successfully applied
    summary: str = ""           # Brief description of action results
    metadata: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)  # Additional action context


class RecoveryReport(BaseModel):
    """Complete report of a recovery attempt.

    Documents everything that happened during a recovery attempt:
    what triggered it, what evidence was collected, what actions were
    taken, and what the final state is.

    Used by RecoveryCoordinator to decide next steps and by operators
    to understand recovery patterns and effectiveness.
    """
    task_id: str                                        # Task that underwent recovery
    origin_stage: str | None = None                     # Stage where failure occurred
    trigger_event_kind: TriggerEventKind               # What triggered recovery
    summary: str                                        # Brief recovery summary
    failure_classification: str | None = None          # Type of failure detected
    runnable_state: Literal["runnable", "parked", "blocked"] = "blocked"  # Final task state after recovery
    blocker: str | None = None                         # What's blocking progress if blocked
    evidence: list[RecoveryEvidenceItem] = Field(default_factory=list)  # Evidence collected during diagnosis
    actions: list[RecoveryAction] = Field(default_factory=list)         # Actions taken during recovery
    warnings: list[str] = Field(default_factory=list) # Non-fatal issues encountered
    recovery_subagent_id: str | None = None            # Subagent that performed recovery
    recovery_subagent_path: str | None = None          # Path where recovery ran
    created_at: str = Field(default_factory=utcnow)   # When recovery report was generated


class ExecutionEstimate(BaseModel):
    """Velocity and ETA estimate for task execution.

    Provides time estimates for task completion based on historical
    execution patterns and current progress. Used by monitoring and
    operator interfaces to understand expected completion times.
    """

    stage_duration_seconds: float = 0.0      # Average time per stage based on history
    remaining_seconds: float = 0.0           # Estimated time to complete remaining work
    velocity_stages_per_hour: float = 0.0    # Rate of stage completion


class TaskActivityEntry(BaseModel):
    """A single entry in the task activity log.

    Represents one entry in the human-readable task history for review
    and conversation. Separate from StageReport to serve different purposes:
    - ActivityEntry: append-only conversation history for humans
    - StageReport: normalized machine-readable summary for routing

    Message is intentionally a free-form string since this object exists
    for human-readable review history. Structured machine data should
    live in dedicated fields on reports and runtime records.
    """

    role: str                                           # Who created this entry (agent role, operator, system)
    stage: str                                          # Pipeline stage where activity occurred
    target_stage: str | None = None                     # Target stage if this is a transition
    verdict: Verdict = Verdict.COMMENT                  # Associated verdict if applicable
    message: str                                        # Free-form human-readable activity description
    files_changed: list[str] = Field(default_factory=list)  # Files modified as part of this activity
    follow_up_task_id: str | None = None                # Optional follow-up task reference
    created_at: str = Field(default_factory=utcnow)    # When the activity occurred

    @model_validator(mode="before")
    @classmethod
    def _normalize_payload(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if "stage" not in normalized and "step" in normalized:
            normalized["stage"] = normalized["step"]
        verdict = normalized.get("verdict")
        if isinstance(verdict, str) and verdict.strip().lower() == "fail":
            normalized["verdict"] = "reject"
        return normalized


__all__ = [
    "ExecutionEstimate",
    "FEEDBACK_CAP",
    "FollowUpTaskSpec",
    "RecoveryAction",
    "RecoveryEvidenceItem",
    "RecoveryReport",
    "StageReport",
    "TaskActivityEntry",
    "TRUNCATION_MARKER",
    "cap_feedback",
]
