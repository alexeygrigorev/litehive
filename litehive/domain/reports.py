"""
Stage reports, recovery reports, activity entries, and the verdict aliases
that bridge submitted-decision strings to the canonical report verdict set.

Two parallel record families live here. ``StageReport`` /
``RecoveryReport`` are the machine-readable summaries the runner persists
for routing; ``TaskActivityEntry`` is the human-readable, append-only
log shown to operators. Keeping both in this module makes it obvious
that the activity log is *not* the routing input — every place that
reads activity for routing should be redirected to the reports.
"""

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from .common import (
    FEEDBACK_CAP,
    OutcomeKind,
    OutcomeReasonCode,
    PipelineState,
    PipelineStatus,
    TaskStage,
    TRUNCATION_MARKER,
    Verdict,
    cap_feedback,
    utcnow,
)
from .recovery import TriggerEventKind


# Stage reports intentionally store a report projection rather than the full
# internal PipelineState. Agent-authored reports belong to executable role
# stages; hook/system states are represented by their owning role stage, except
# merge and recovery reports which need their own explicit labels.
ReportPipelineState: TypeAlias = TaskStage | Literal[PipelineState.MERGE_RESOLVING, PipelineState.RECOVERING]
# Activity entries can be authored at every stage a report can be authored at,
# plus the operator-facing pipeline buckets (``backlog``, ``done``, ``flagged``)
# the runner stamps on entries that aren't tied to an executable stage. The
# union covers both without collapsing back to ``str``.
TaskActivityStage: TypeAlias = ReportPipelineState | PipelineStatus
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
_STAGE_REPORT_VERDICT_ALIASES: dict[Verdict, StageReportVerdict] = {
    Verdict.PASS: "pass",
    Verdict.ACCEPT: "pass",
    Verdict.RESUME: "pass",
    Verdict.ADVANCE: "pass",
    Verdict.DONE: "pass",
    Verdict.REJECT: "reject",
    Verdict.FAIL: "reject",
    Verdict.BLOCKED: "blocked",
    Verdict.BUDGET_HIT: "blocked",
}


def classify_task_activity_verdict(role: str, verdict: str) -> str | None:
    """
    Return the first-class classification for a newly submitted verdict.

    Specifically tags QA/reviewer rejects as ``semantic_reject`` so the
    recovery budget can group judgement-based rejects separately from
    hook-driven ones — those are the rejects that retry exhaustion
    treats as terminal under ``has_blocking_failed_run_history``.
    Returns ``None`` for verdicts that don't need a classification.
    """
    if verdict.strip().lower() == "reject" and role.strip().lower() in SEMANTIC_REJECT_ROLES:
        return SEMANTIC_REJECT_CLASSIFICATION
    return None


def canonical_stage_report_verdict(verdict: Verdict | str) -> StageReportVerdict | None:
    """
    Map a submitted activity verdict to the canonical StageReport verdict.

    Activity entries carry a wider vocabulary (``advance``, ``done``,
    ``budget_hit``…) but stage reports only persist
    ``pass``/``reject``/``blocked``. Returns ``None`` when the input
    verdict has no report mapping (e.g. ``comment``) so the caller can
    distinguish "report this" from "log this for humans only".
    """
    try:
        verdict_kind = verdict if isinstance(verdict, Verdict) else Verdict(verdict.strip().lower())
    except ValueError:
        return None
    return _STAGE_REPORT_VERDICT_ALIASES.get(verdict_kind)


# Activity-entry verdicts that count as a CLI-submitted stage report.
# Excludes "comment", which is operator/agent commentary that does not
# advance the stage report.
REPORT_VERDICT_KINDS: frozenset[TaskActivityVerdict] = frozenset(
    {"pass", "reject", "blocked", "resume", "advance", "done", "budget_hit"}
)


def canonical_report_pipeline_state(value: str | TaskStage) -> ReportPipelineState:
    """
    Convert a stage label to the typed ``ReportPipelineState``.

    Accepts the string spelling used by submitting callers
    (``"implementing"``, ``"merge_resolving"``) and returns either the
    matching :class:`TaskStage` member or one of the two ``PipelineState``
    extensions allowed on stage reports (``MERGE_RESOLVING``,
    ``RECOVERING``). Raises ``ValueError`` on unknown spellings —
    there is no "unknown" fallback because storing a bad value would
    silently break every consumer that filters by stage.
    """
    if isinstance(value, TaskStage):
        return value
    text = str(value)
    if text == PipelineState.MERGE_RESOLVING.value:
        return PipelineState.MERGE_RESOLVING
    if text == PipelineState.RECOVERING.value:
        return PipelineState.RECOVERING
    return TaskStage(text)


class StageReport(BaseModel):
    """
    Normalized, machine-readable summary of one pipeline-state execution.

    Distinct from ``TaskActivityEntry``: activity is the append-only
    human history, while ``StageReport`` is the routing input
    ``PipelineRunner`` reads to decide pass/reject/blocked. Built by
    ``stage_report_from_subagent`` from ``litehive agent report`` CLI
    submissions; the legacy ``STAGE_RESULT: <yaml>`` parsing path is
    gone.

    ``failure_diagnostics`` is report-local evidence and may seed a
    later ``FailureFingerprint`` — but the report does not own
    recovery identity. The recovery vocabulary lives in
    ``litehive.domain.recovery``.
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
    """
    Spec for a follow-up task spawned by a running stage.

    An agent decides during a stage that side work needs its own task
    — and emits one of these to the runner, which then creates the
    actual ``TaskRecord``. ``blocking=True`` means the parent should
    wait on the follow-up (encoded later via
    :func:`blocked_on_follow_up_reason`); ``blocking=False`` lets the
    parent finish independently while the follow-up queues.
    """

    title: str  # Brief title for the follow-up task
    rationale: str  # Why this follow-up task is needed
    blocking: bool = False  # Whether this blocks the current task
    goal: str = ""  # Main objective of the follow-up task
    acceptance_criteria: list[str] = Field(default_factory=list)  # Completion conditions


class RecoveryEvidenceItem(BaseModel):
    """
    One piece of evidence recorded by the recovery agent.

    The recovery agent's prompt asks it to enumerate what it inspected
    (logs, files, status output, …) and surface each as evidence so a
    later operator can audit the diagnosis. Stored on
    ``RecoveryReport.evidence``; never used for routing — purely for
    audit and debugging.
    """

    kind: str  # Type of evidence (file, log, state, etc.)
    label: str  # Human-readable description
    path: str | None = None  # Filesystem path if applicable
    exists: bool = False  # Whether the evidence was found/valid
    summary: str = ""  # Brief summary of findings
    metadata: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)  # Additional context


class RecoveryAction(BaseModel):
    """
    One action the recovery agent reports it performed.

    Companion to ``RecoveryEvidenceItem``: actions describe what the
    agent *changed* (files edited, scripts run, deps installed) while
    evidence describes what it *looked at*. Stored on
    ``RecoveryReport.actions`` for the operator-facing audit trail.
    """

    action: str  # Description of the recovery action taken
    summary: str = ""  # Brief description of action results
    metadata: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)  # Additional action context


class RecoveryReport(BaseModel):
    """
    Complete machine-readable record of a recovery attempt.

    Built by the recovery agent submission path; the lifecycle layer
    converts it into the lighter ``RecoveryOutcome`` for the history
    list and uses ``runnable_state`` to decide whether the task is
    safe to resume. The full report stays in storage for the
    operator's audit view; the lifecycle layer only needs the
    summary.
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
    """
    ETA projection for an in-flight task.

    Computed from historical stage durations and the task's current
    position; rendered by status output so an operator can decide
    whether to wait, switch tasks, or interrupt. Velocity is in
    stages-per-hour because that's the unit operators reason about
    when comparing engines, not seconds-per-token.
    """

    stage_duration_seconds: float = 0.0  # Average time per stage based on history
    remaining_seconds: float = 0.0  # Estimated time to complete remaining work
    velocity_stages_per_hour: float = 0.0  # Rate of stage completion


class TaskActivityEntry(BaseModel):
    """
    One row of the human-readable task activity log.

    Append-only conversation/decision history shown in the operator's
    task view. Carries a free-form ``message`` because the audience is
    a human reading back through the run; structured machine data
    belongs on ``StageReport`` / runtime records, not here. Routing
    code that reads from activity is a code smell — that's what
    ``StageReport`` is for.
    """

    role: str  # Who created this entry (agent role, operator, system)
    stage: TaskActivityStage  # Pipeline stage where activity occurred
    target_stage: TaskActivityStage | None = None  # Target stage if this is a transition
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
    "TaskActivityStage",
    "TRUNCATION_MARKER",
    "cap_feedback",
    "canonical_report_pipeline_state",
    "canonical_stage_report_verdict",
    "classify_task_activity_verdict",
]
