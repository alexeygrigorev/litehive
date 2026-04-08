"""Runtime and execution state models."""

from typing import Literal

from pydantic import BaseModel, Field

from .common import (
    OutcomeKind,
    OutcomeReasonCode,
    RetrySource,
    RunnerExecutionStatus,
    SubagentStatus,
    utcnow,
)


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
