"""Runtime and execution state models.

ResourceLimitEvent and RuntimeEngineContinuation now live in heru.types.
This module re-exports them and keeps the litehive-only runtime state
models (RuntimeGitState, RuntimeStageState, etc.) authoritative here.
"""

from typing import Literal

from pydantic import AliasChoices, BaseModel, Field

from litehive.domain._heru_compat import (
    ResourceLimitEvent,
    RuntimeEngineContinuation,
    SubagentRef as HeruSubagentRef,
)

from .common import (
    OutcomeKind,
    OutcomeReasonCode,
    RunnerExecutionStatus,
    SubagentStatus,
    utcnow,
)


class RuntimeGitState(BaseModel):
    commit_sha: str | None = None
    worktree_path: str | None = None


class RuntimeStageState(BaseModel):
<<<<<<< HEAD
    stage: str | None = None
=======
    step: str | None = Field(default=None, validation_alias=AliasChoices("step", "stage"))
>>>>>>> 61b0a5fa (litehive T-0398: auto-commit worktree changes)
    status: str = "idle"
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str | None = None
    duration_seconds: int = 0
    verdict: str | None = None
    summary: str = ""

    @property
    def stage(self) -> str | None:
        return self.step

    @stage.setter
    def stage(self, value: str | None) -> None:
        self.step = value


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


SubagentRef = HeruSubagentRef


class RuntimeEngineSwitch(BaseModel):
    stage: str
    from_engine: str
    to_engine: str
    reason: str
    happened_at: str = Field(default_factory=utcnow)


class RuntimeHookRejectFingerprint(BaseModel):
    point: str
    command: str
    description: str = ""
    fingerprint: str


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
    stage: str
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
    stage_retry_counts: dict[str, int] = Field(default_factory=dict)
    current_stage: RuntimeStageState = Field(default_factory=RuntimeStageState)
    last_stage: RuntimeStageState = Field(default_factory=RuntimeStageState)
    active_subagent: RuntimeSubagentState | None = None
    last_subagent: RuntimeSubagentState | None = None
    interruption: RuntimeInterruptionState | None = None
    continuation_handoff: RuntimeContinuationHandoff | None = None
    last_engine_switch: RuntimeEngineSwitch | None = None
    consecutive_same_hook_rejects: int = 0
    last_hook_reject_fingerprint: RuntimeHookRejectFingerprint | None = None
    hook_reject_recovery_invoked: bool = False
    last_outcome: TaskOutcomeState = Field(default_factory=TaskOutcomeState)
    self_heal_traceback_fingerprints: list[str] = Field(default_factory=list)

    def for_storage(
        self,
        *,
        commit_sha: str | None,
        worktree_path: str | None,
    ) -> "TaskRuntime":
        runtime = self.model_copy(deep=True)
        runtime.git.commit_sha = commit_sha
        runtime.git.worktree_path = worktree_path
        return runtime


class RunnerStatusState(BaseModel):
    status: RunnerExecutionStatus = RunnerExecutionStatus.IDLE
    pid: int | None = None
    workspace: str = ""
    command: str = ""
    started_at: str | None = None
    heartbeat_at: str | None = None
    active_task_id: str | None = None
