"""Core YAML-backed models for litehive."""


from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


TaskMode = Literal["tasks", "implementation"]
PipelineMode = Literal["single", "full"]
TaskComplexity = Literal["simple", "moderate", "complex"]
PlannedEffort = Literal["xs", "s", "m", "l", "xl"]
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
SubagentStatus = Literal["created", "running", "completed", "failed", "blocked", "interrupted"]
RunnerExecutionStatus = Literal["idle", "running", "late", "stale"]
OutcomeKind = Literal[
    "flagged",
    "blocked",
    "interrupted",
    "cancelled",
    "wont_do",
    "deferred",
    "duplicate",
]
RetrySource = Literal["global", "task"]
HumanCheckpoint = Literal["before_acceptance", "before_commit"]
UpstreamContributionKind = Literal[
    "runtime_bug",
    "missing_feature",
    "config_improvement",
    "prompt_improvement",
    "engine_adapter_fix",
]
OutcomeReasonCode = Literal[
    "verdict_fail",
    "verdict_reject",
    "verdict_blocked",
    "resource_limit",
    "missing_acceptance_criteria",
    "retry_limit_exhausted",
    "stage_retry_limit_exhausted",
    "execution_interrupted",
    "execution_cancelled",
    "stage_exception",
    "unsupported_verdict",
    "merge_conflict",
    # intentional non-implementation outcomes set via `litehive close`
    "wont_do",
    "deferred",
    "duplicate",
]
EngineMonitoringSource = Literal["provider", "local"]
EngineLimitKind = Literal["quota", "rate", "budget", "capacity"]
LiveEventKind = Literal[
    "message",
    "tool_call",
    "tool_result",
    "error",
    "usage",
    "status",
]
LiveEventRole = Literal["assistant", "user", "system"]


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class SubagentRef(BaseModel):
    id: str
    role: str
    engine: str
    status: SubagentStatus = "created"
    path: str
    sandboxed: bool = False
    sandbox_summary: str = ""


class ResourceLimitEvent(BaseModel):
    resource: Literal["memory", "cpu", "processes", "resource"] = "resource"
    reason: str
    observed_signal: str | None = None
    exit_code: int | None = None
    memory_mb: int | None = None
    cpu_count: float | None = None
    process_limit: int | None = None


class RuntimeGitState(BaseModel):
    commit_sha: str | None = None
    worktree_path: str | None = None


class RuntimeStageState(BaseModel):
    step: str | None = None
    status: str = "idle"
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str | None = None
    duration_seconds: int = 0
    verdict: str | None = None
    summary: str = ""


class RuntimeEngineContinuation(BaseModel):
    session_id: str | None = None
    thread_id: str | None = None
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=utcnow)


class RuntimeSubagentState(BaseModel):
    id: str
    role: str
    engine: str
    status: SubagentStatus
    path: str
    pid: int | None = None
    sandboxed: bool = False
    sandbox_summary: str = ""
    started_at: str
    updated_at: str
    completed_at: str | None = None
    exit_code: int | None = None
    transcript_snippet: str = ""
    interruption_reason: str = ""
    resource_limit_event: ResourceLimitEvent | None = None
    continuation: RuntimeEngineContinuation | None = None


class RuntimeEngineSwitch(BaseModel):
    step: str
    from_engine: str
    to_engine: str
    reason: str
    happened_at: str = Field(default_factory=utcnow)


class EngineUsageWindow(BaseModel):
    used: int | None = None
    limit: int | None = None
    remaining: int | None = None
    unit: str | None = None
    reset_at: str | None = None


class EngineUsageObservation(BaseModel):
    source: EngineMonitoringSource = "local"
    provider: str | None = None
    observed_at: str = Field(default_factory=utcnow)
    invocation_count: int = 1
    success: bool | None = None
    limit_reason: str | None = None
    limit_kind: EngineLimitKind | None = None
    usage: EngineUsageWindow | None = None
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)


class EngineUsageRecord(BaseModel):
    engine: str
    source: EngineMonitoringSource = "local"
    provider: str | None = None
    observed_at: str | None = None
    last_invoked_at: str | None = None
    last_task_id: str | None = None
    last_exit_code: int | None = None
    invocation_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    limit_event_count: int = 0
    last_limit_reason: str | None = None
    last_limit_kind: EngineLimitKind | None = None
    usage: EngineUsageWindow | None = None
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)


class WorkspaceEngineMonitoring(BaseModel):
    engines: dict[str, EngineUsageRecord] = Field(default_factory=dict)


class TaskRetryPolicy(BaseModel):
    max_retries: int | None = None
    stage_retry_limit: int | None = None


class TaskCreationSource(BaseModel):
    task_id: str
    stage: Literal["grooming", "accepting"]
    rationale: str
    blocking: bool = False


class UpstreamPatchProposal(BaseModel):
    branch: str | None = None
    base_ref: str | None = None
    prepared: bool = False
    repo_path: str | None = None


class UpstreamContributionOrigin(BaseModel):
    source_project: str
    source_workspace: str
    source_task_id: str | None = None
    source_task_title: str | None = None
    source_stage: str | None = None
    source_role: str | None = None
    contribution_kind: UpstreamContributionKind
    summary: str = ""
    details: str = ""
    litehive_source_path: str
    patch: UpstreamPatchProposal | None = None


class FollowUpTaskSpec(BaseModel):
    title: str
    rationale: str
    blocking: bool = False
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    task_type: str | None = None


class StageResultTests(BaseModel):
    added: int = 0
    passing: int = 0


class TaskUpdateSubmission(BaseModel):
    """Structured task updates submitted by agents during grooming."""

    goal: str | None = None
    acceptance_criteria: list[str] | None = None
    constraints: list[str] | None = None
    plan: list[str] | None = None
    pm_complexity: TaskComplexity | None = None
    planned_effort: PlannedEffort | None = None
    depends_on: list[str] | None = None
    human_checkpoints: list[HumanCheckpoint] | None = None
    task_type: str | None = None
    mode: TaskMode | None = None
    priority: str | None = None
    engine: str | None = None
    model: str | None = None
    retry_limit: int | None = None
    auto_commit: bool | None = None
    outcome: TaskStatus | None = None
    outcome_reason: str | None = None
    action: Literal["park", "requeue", "abandon"] | None = None


class StageResultSubmission(BaseModel):
    """Schema-validated structured stage result submitted by agents.

    Agents emit a ``STAGE_RESULT:`` JSON block in their transcript.
    This model validates the payload before converting it to a StageReport.
    Extra keys are forbidden so agents get clear validation errors.
    """

    model_config = {"extra": "forbid"}

    verdict: Literal["pass", "accept", "fail", "reject", "blocked"]
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    tests: StageResultTests = Field(default_factory=StageResultTests)
    warnings: list[str] = Field(default_factory=list)
    follow_up_tasks: list[FollowUpTaskSpec] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    task_update: TaskUpdateSubmission | None = None


class RecoveryEvidenceItem(BaseModel):
    kind: str
    label: str
    path: str | None = None
    exists: bool = False
    summary: str = ""
    metadata: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)


class RecoveryAction(BaseModel):
    action: str
    applied: bool = True
    summary: str = ""
    metadata: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)


class RecoveryReport(BaseModel):
    task_id: str
    stage: str | None = None
    trigger: str
    summary: str
    failure_classification: str | None = None
    runnable_state: Literal["runnable", "parked", "blocked"] = "blocked"
    blocker: str | None = None
    evidence: list[RecoveryEvidenceItem] = Field(default_factory=list)
    actions: list[RecoveryAction] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    recovery_subagent_id: str | None = None
    recovery_subagent_path: str | None = None
    created_at: str = Field(default_factory=utcnow)


class TaskOutcomeState(BaseModel):
    kind: OutcomeKind | None = None
    stage: str | None = None
    reason_code: OutcomeReasonCode | None = None
    reason: str = ""
    failure_classification: str | None = None
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)
    follow_up_task_id: str | None = None
    retry_count: int = 0
    retry_limit: int = 0
    retry_source: RetrySource = "global"
    recorded_at: str | None = None


class RuntimeInterruptionState(BaseModel):
    source: Literal["runner", "subagent"] = "runner"
    stage: str | None = None
    pipeline_status: str | None = None
    resume_stage: str | None = None
    reason: str = ""
    summary: str = ""
    interrupted_at: str | None = None
    detected_at: str | None = None
    run_started_at: str | None = None
    stage_started_at: str | None = None
    subagent: RuntimeSubagentState | None = None


class RuntimeContinuationHandoff(BaseModel):
    step: str
    kind: Literal["retry", "engine_switch", "restart"]
    reason: str
    from_engine: str | None = None
    to_engine: str | None = None
    from_model: str | None = None
    to_model: str | None = None
    subagent_id: str | None = None
    subagent_path: str | None = None
    status: str | None = None
    attempt: int | None = None
    summary: str = ""
    transcript_snippet: str = ""
    warnings: list[str] = Field(default_factory=list)
    session_path: str | None = None
    report_path: str | None = None
    transcript_path: str | None = None
    continuation: RuntimeEngineContinuation | None = None
    updated_at: str = Field(default_factory=utcnow)


class TaskRuntime(BaseModel):
    git: RuntimeGitState = Field(default_factory=RuntimeGitState)
    execution_status: str = "idle"
    run_started_at: str | None = None
    updated_at: str | None = None
    retry_count: int = 0
    retry_limit: int = 0
    retry_source: RetrySource = "global"
    stage_retry_counts: dict[str, int] = Field(default_factory=dict)
    current_stage: RuntimeStageState = Field(default_factory=RuntimeStageState)
    last_stage: RuntimeStageState = Field(default_factory=RuntimeStageState)
    active_subagent: RuntimeSubagentState | None = None
    last_subagent: RuntimeSubagentState | None = None
    interruption: RuntimeInterruptionState | None = None
    continuation_handoff: RuntimeContinuationHandoff | None = None
    last_engine_switch: RuntimeEngineSwitch | None = None
    last_outcome: TaskOutcomeState = Field(default_factory=TaskOutcomeState)
    self_heal_traceback_fingerprints: list[str] = Field(default_factory=list)


class RunnerStatusState(BaseModel):
    status: RunnerExecutionStatus = "idle"
    pid: int | None = None
    workspace: str = ""
    command: str = ""
    started_at: str | None = None
    heartbeat_at: str | None = None
    active_task_id: str | None = None


class GitSettings(BaseModel):
    auto_commit: bool = True
    commit_message: str | None = None
    commit_sha: str | None = None
    checkpoint_base_sha: str | None = None
    checkpoint_attempts: int = 0
    rolled_back_checkpoint_attempt: int | None = None
    worktree_path: str | None = None


class TaskRecord(BaseModel):
    id: str
    slug: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
    task_type: str | None = None
    engine: str | None = None
    model: str | None = None
    mode: TaskMode = "implementation"
    pipeline_mode: PipelineMode = "full"
    status: TaskStatus = "queued"
    pipeline_status: PipelineStatus = "backlog"
    priority: str = "medium"
    pm_complexity: TaskComplexity | None = None
    planned_effort: PlannedEffort | None = None
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    human_checkpoints: list[HumanCheckpoint] = Field(default_factory=list)
    subagents: list[SubagentRef] = Field(default_factory=list)
    git: GitSettings = Field(default_factory=GitSettings)
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)
    created_from: TaskCreationSource | None = None
    upstream_origin: UpstreamContributionOrigin | None = None
    runtime: TaskRuntime = Field(default_factory=TaskRuntime, exclude=True)


class UnmergedWorktree(BaseModel):
    task_id: str
    worktree_path: str


class WorkspaceState(BaseModel):
    active_task_id: str | None = None
    mode: TaskMode = "implementation"
    queue: list[str] = Field(default_factory=list)
    pool_stop_reason: str | None = None
    next_task_number: int = 0
    unmerged_worktrees: list[UnmergedWorktree] = Field(default_factory=list)


class LiveEvent(BaseModel):
    kind: LiveEventKind
    engine: str
    sequence: int = 0
    timestamp: str = Field(default_factory=utcnow)
    role: LiveEventRole | None = None
    content: str = ""
    tool_name: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    error: str | None = None
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)


class LiveTimeline(BaseModel):
    events: list[LiveEvent] = Field(default_factory=list)
    engine: str = ""
    task_id: str | None = None
    subagent_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    event_counts: dict[str, int] = Field(default_factory=dict)

    def recompute_counts(self) -> None:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[event.kind] = counts.get(event.kind, 0) + 1
        self.event_counts = counts


class StageReport(BaseModel):
    task_id: str
    step: Literal["grooming", "implementing", "testing", "accepting", "commit_to_git"]
    verdict: Literal["pass", "accept", "fail", "reject", "blocked"]
    summary: str
    feedback: str = ""
    files_changed: list[str] = Field(default_factory=list)
    follow_up_tasks: list[FollowUpTaskSpec] = Field(default_factory=list)
    created_follow_up_task_ids: list[str] = Field(default_factory=list)
    task_update: dict[str, object] = Field(default_factory=dict)
    tests: dict[str, int] = Field(default_factory=lambda: {"added": 0, "passing": 0})
    warnings: list[str] = Field(default_factory=list)
    retry_count: int = 0
    retry_limit: int = 0
    retry_source: RetrySource = "global"
    retry_decision: Literal["continue", "retry", "final"] = "continue"
    outcome: OutcomeKind | None = None
    outcome_reason_code: OutcomeReasonCode | None = None
    outcome_reason: str = ""
    failure_classification: str | None = None
    failure_diagnostics: dict[str, str | int | bool | None | list[str]] = Field(default_factory=dict)
    resource_limit_event: ResourceLimitEvent | None = None
    duration_seconds: int = 0
    hook_results: list[dict[str, str | int | bool | None]] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)


FEEDBACK_CAP = 2000
_TRUNCATION_MARKER = "\n\n… [truncated — full transcript in subagent artifacts]"


def cap_feedback(text: str, *, limit: int = FEEDBACK_CAP) -> str:
    """Truncate feedback to *limit* characters, appending a marker if trimmed."""
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


class ExecutionEstimate(BaseModel):
    """Velocity and ETA estimate for task execution."""

    stage_duration_seconds: float = 0.0
    remaining_seconds: float = 0.0
    velocity_stages_per_hour: float = 0.0


class TaskThreadComment(BaseModel):
    """A single comment in the task discussion thread."""

    role: str  # swe, qa, reviewer, planner, merge-resolver, ...
    step: str  # grooming, implementing, testing, accepting, commit_to_git
    verdict: Literal["pass", "fail", "reject", "blocked", "comment"] = "comment"
    message: str
    files_changed: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)
