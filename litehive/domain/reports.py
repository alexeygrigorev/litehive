"""Stage, recovery, and reporting models (litehive-native)."""

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from .common import (
    FEEDBACK_CAP,
    OutcomeKind,
    OutcomeReasonCode,
    TaskStage,
    TRUNCATION_MARKER,
    cap_feedback,
    utcnow,
)
from .recovery import TriggerEventKind


# Stage reports intentionally store a report projection rather than the full
# internal PipelineState. Agent-authored reports belong to executable role
# stages; hook/system states are represented by their owning role stage, except
# merge and recovery reports which need their own explicit labels.
ReportPipelineState: TypeAlias = TaskStage | Literal["merge_resolving", "recovering"]
StageReportVerdict: TypeAlias = Literal["pass", "reject", "blocked"]
TaskActivityVerdict: TypeAlias = Literal[
    "pass",
    "reject",
    "blocked",
    "comment",
    "resume",
    "advance",
    "done",
    "budget_hit",
]

SEMANTIC_REJECT_CLASSIFICATION = "semantic_reject"
SEMANTIC_REJECT_ROLES = frozenset({"qa", "reviewer"})
_STAGE_REPORT_VERDICT_ALIASES: dict[str, StageReportVerdict] = {
    "pass": "pass",
    "accept": "pass",
    "resume": "pass",
    "advance": "pass",
    "done": "pass",
    "reject": "reject",
    "fail": "reject",
    "blocked": "blocked",
    "budget_hit": "blocked",
}


def classify_task_activity_verdict(role: str, verdict: str) -> str | None:
    """Return the first-class classification for a newly submitted verdict."""
    if verdict.strip().lower() == "reject" and role.strip().lower() in SEMANTIC_REJECT_ROLES:
        return SEMANTIC_REJECT_CLASSIFICATION
    return None


def canonical_stage_report_verdict(verdict: str) -> StageReportVerdict | None:
    """Map submitted activity verdicts into the canonical StageReport verdict set."""
    return _STAGE_REPORT_VERDICT_ALIASES.get(verdict.strip().lower())


# Activity-entry verdicts that count as a CLI-submitted stage report.
# Excludes "comment", which is operator/agent commentary that does not
# advance the stage report.
REPORT_VERDICT_KINDS: frozenset[TaskActivityVerdict] = frozenset(
    {"pass", "reject", "blocked", "resume", "advance", "done", "budget_hit"}
)


_REPORT_PIPELINE_STATE_LITERALS: frozenset[str] = frozenset({"merge_resolving", "recovering"})


def canonical_report_pipeline_state(value: str | TaskStage) -> ReportPipelineState:
    """Convert a stage label to the typed ``ReportPipelineState``.

    Accepts the string used by callers (e.g. ``"implementing"``,
    ``"merge_resolving"``) and returns either the matching
    :class:`TaskStage` member or one of the literal extensions allowed
    on stage reports. Raises :class:`ValueError` otherwise — there is
    no fallback "unknown" stage.
    """
    if isinstance(value, TaskStage):
        return value
    text = str(value)
    if text in _REPORT_PIPELINE_STATE_LITERALS:
        return text  # type: ignore[return-value]  # narrowed to literal by membership check
    return TaskStage(text)


class StageReport(BaseModel):
    """Normalized machine-readable summary of a pipeline state execution.

    Separate from ActivityEntry to serve different purposes:
    - ActivityEntry: append-only conversation and review history for humans
    - StageReport: normalized machine-readable summary for routing, reporting,
      and later analysis by PipelineRunner and recovery logic

    Historically heru parsed a `STAGE_RESULT: <yaml>` block out of agent
    stdout to build one of these. That path is gone — agents now submit
    verdicts via the `litehive agent report` CLI and `stage_report_from_subagent`
    constructs this record directly.

    Primary consumers: PipelineRunner for routing decisions, recovery logic
    for failure analysis, and reporting systems for metrics and debugging.

    ``failure_diagnostics`` is report-local evidence about this stage verdict.
    It can help construct a ``FailureFingerprint`` later, but it is not the
    recovery-domain identity or budget key.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str  # Task this report belongs to
    pipeline_state: ReportPipelineState  # Report projection for the executed pipeline state
    verdict: StageReportVerdict  # Final report verdict: pass, reject, or blocked
    source: Literal["agent", "hook"] = "agent"  # What generated this report
    summary: str  # Brief description of pipeline-state results
    feedback: str = ""  # Detailed feedback or explanation
    submitted_via_cli: bool = False  # Whether submitted via CLI vs internal
    tests: dict[str, int] = Field(default_factory=lambda: {"added": 0, "passing": 0})  # Test metrics
    warnings: list[str] = Field(default_factory=list)  # Non-fatal warnings
    retry_count: int = 0  # Current retry attempt number
    retry_limit: int = 0  # Maximum retries allowed
    outcome: OutcomeKind | None = None  # Terminal outcome if stage completed task
    outcome_reason_code: OutcomeReasonCode | None = None  # Machine-readable outcome reason
    failure_classification: str | None = None  # Type of failure if applicable
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)  # Report evidence
    duration_seconds: int = 0  # How long stage execution took
    created_at: str = Field(default_factory=utcnow)  # When report was generated


class FollowUpTaskSpec(BaseModel):
    """Specification for creating a follow-up task.

    Used when a stage execution determines that additional work is needed
    that should be tracked as a separate task. Captures the intent and
    requirements for the follow-up task creation.
    """

    title: str  # Brief title for the follow-up task
    rationale: str  # Why this follow-up task is needed
    blocking: bool = False  # Whether this blocks the current task
    goal: str = ""  # Main objective of the follow-up task
    acceptance_criteria: list[str] = Field(default_factory=list)  # Completion conditions


class RecoveryEvidenceItem(BaseModel):
    """Evidence collected during recovery diagnosis.

    Represents a piece of information gathered by the recovery agent
    to understand the failure context and determine appropriate recovery
    actions. Used to document what was checked during recovery.
    """

    kind: str  # Type of evidence (file, log, state, etc.)
    label: str  # Human-readable description
    path: str | None = None  # Filesystem path if applicable
    exists: bool = False  # Whether the evidence was found/valid
    summary: str = ""  # Brief summary of findings
    metadata: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)  # Additional context


class RecoveryAction(BaseModel):
    """Action taken during recovery to address a failure.

    Documents what the recovery agent did to attempt to resolve the
    issue that triggered recovery. Used for recovery audit trails
    and understanding recovery effectiveness.
    """

    action: str  # Description of the recovery action taken
    summary: str = ""  # Brief description of action results
    metadata: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)  # Additional action context


class RecoveryReport(BaseModel):
    """Complete report of a recovery attempt.

    Documents everything that happened during a recovery attempt:
    what triggered it, what evidence was collected, what actions were
    taken, and what the final state is.

    Used by RecoveryCoordinator to decide next steps and by operators
    to understand recovery patterns and effectiveness.
    """

    task_id: str  # Task that underwent recovery
    origin_stage: str | None = None  # Stage where failure occurred
    trigger_event_kind: TriggerEventKind  # What triggered recovery
    summary: str  # Brief recovery summary
    failure_classification: str | None = None  # Type of failure detected
    runnable_state: Literal["runnable", "parked", "blocked"] = "blocked"  # Final task state after recovery
    blocker: str | None = None  # What's blocking progress if blocked
    evidence: list[RecoveryEvidenceItem] = Field(default_factory=list)  # Evidence collected during diagnosis
    actions: list[RecoveryAction] = Field(default_factory=list)  # Actions taken during recovery
    warnings: list[str] = Field(default_factory=list)  # Non-fatal issues encountered
    created_at: str = Field(default_factory=utcnow)  # When recovery report was generated


class ExecutionEstimate(BaseModel):
    """Velocity and ETA estimate for task execution.

    Provides time estimates for task completion based on historical
    execution patterns and current progress. Used by monitoring and
    operator interfaces to understand expected completion times.
    """

    stage_duration_seconds: float = 0.0  # Average time per stage based on history
    remaining_seconds: float = 0.0  # Estimated time to complete remaining work
    velocity_stages_per_hour: float = 0.0  # Rate of stage completion


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

    role: str  # Who created this entry (agent role, operator, system)
    stage: str  # Pipeline stage where activity occurred
    target_stage: str | None = None  # Target stage if this is a transition
    verdict: TaskActivityVerdict = "comment"  # Associated verdict if applicable
    verdict_classification: str | None = None  # Machine-readable verdict classification
    message: str  # Free-form human-readable activity description
    files_changed: list[str] = Field(default_factory=list)  # Files modified as part of this activity
    source_subagent_id: str | None = None  # Subagent session that submitted this entry, when applicable
    follow_up_task_id: str | None = None  # Optional follow-up task reference
    created_at: str = Field(default_factory=utcnow)  # When the activity occurred


__all__ = [
    "ExecutionEstimate",
    "FEEDBACK_CAP",
    "FollowUpTaskSpec",
    "RecoveryAction",
    "RecoveryEvidenceItem",
    "RecoveryReport",
    "REPORT_VERDICT_KINDS",
    "ReportPipelineState",
    "SEMANTIC_REJECT_CLASSIFICATION",
    "StageReport",
    "StageReportVerdict",
    "TaskActivityEntry",
    "TRUNCATION_MARKER",
    "cap_feedback",
    "canonical_report_pipeline_state",
    "canonical_stage_report_verdict",
    "classify_task_activity_verdict",
]
