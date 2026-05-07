"""
Subagent role vocabulary and its stage relationships.

Roles answer "which kind of agent is acting"; stages answer "which
pipeline bucket owns the work". Keeping the fallback stage on the role
object prevents runner code from carrying its own role/stage lookup
table when it needs to seed LITEHIVE_STAGE before a task has entered a
runtime stage.
"""

from litehive.domain.common import (
    PipelineState,
    StringEnum,
    TaskStage,
    Verdict,
    task_stage_for_pipeline_state,
)
from litehive.domain.reports import ReportPipelineState, TaskActivityVerdict
from litehive.domain.task import TaskRecord


_DEFAULT_AGENT_ACTIVITY_VERDICTS: frozenset[TaskActivityVerdict] = frozenset({Verdict.PASS, Verdict.REJECT})
_RECOVERY_AGENT_ACTIVITY_VERDICTS: frozenset[TaskActivityVerdict] = frozenset(
    {
        Verdict.RESUME,
        Verdict.ADVANCE,
        Verdict.DONE,
        Verdict.BUDGET_HIT,
        Verdict.REJECT,
    }
)
_RECOVERY_TARGET_STAGE_VERDICTS: frozenset[TaskActivityVerdict] = frozenset({Verdict.RESUME, Verdict.ADVANCE})


class AgentRole(StringEnum):
    """
    Canonical subagent roles that have lifecycle-owned stage defaults.

    Free-form role strings still exist at adapter and persistence
    boundaries, but once a known role is recognized this enum owns the
    domain facts attached to it. The manager uses default_stage only as
    a fallback when task runtime has not yet recorded a current stage.
    """

    PLANNER = "planner"
    SWE = "swe"
    QA = "qa"
    REVIEWER = "reviewer"
    MERGE_RESOLVER = "merge-resolver"
    RECOVERY = "recovery"

    @property
    def default_stage(self) -> ReportPipelineState:
        """
        Stage exported for this role before runtime state is available.

        Normal stage roles report to their user-facing TaskStage.
        Merge resolution and recovery are executable pseudo-stages, so
        they intentionally keep the internal PipelineState label that
        report storage accepts.
        """
        match self:
            case AgentRole.PLANNER:
                return TaskStage.GROOMING
            case AgentRole.SWE:
                return TaskStage.IMPLEMENTING
            case AgentRole.QA:
                return TaskStage.TESTING
            case AgentRole.REVIEWER:
                return TaskStage.ACCEPTING
            case AgentRole.MERGE_RESOLVER:
                return PipelineState.MERGE_RESOLVING
            case AgentRole.RECOVERY:
                return PipelineState.RECOVERING

    @property
    def allowed_activity_verdicts(self) -> frozenset[TaskActivityVerdict]:
        """
        Verdicts this role may submit through the agent report channel.
        """
        if self is AgentRole.RECOVERY:
            return _RECOVERY_AGENT_ACTIVITY_VERDICTS
        return _DEFAULT_AGENT_ACTIVITY_VERDICTS


def agent_startup_guidance_keys() -> frozenset[str]:
    """
    Return config keys that may receive startup guidance overlays.

    Normal task roles and recovery accept operator guidance. Merge
    resolution is intentionally excluded because it is a narrow
    system repair role rather than a configurable task-stage agent.
    """
    guidance_roles = {
        AgentRole.PLANNER,
        AgentRole.SWE,
        AgentRole.QA,
        AgentRole.REVIEWER,
        AgentRole.RECOVERY,
    }
    return frozenset({"all", *(role.value for role in guidance_roles)})


def known_agent_role(value: str | None) -> AgentRole | None:
    """
    Convert a persisted or adapter-supplied role string when it is known.

    Unknown role names are allowed at the boundary so experimental
    adapters can still run, but they do not get a special stage default.
    Callers fall back to the implementation stage for those roles.
    """
    if value is None:
        return None
    try:
        return AgentRole(value)
    except ValueError:
        return None


def agent_activity_verdicts_for_role(role: str | None) -> frozenset[TaskActivityVerdict]:
    """
    Return the agent-report verdict gate for a persisted role label.

    Unknown roles get the conservative pass/reject surface; only the
    recovery role may submit resume/advance/done/budget-hit routing
    verdicts.
    """
    agent_role = known_agent_role(role)
    if agent_role is None:
        return _DEFAULT_AGENT_ACTIVITY_VERDICTS
    return agent_role.allowed_activity_verdicts


def agent_verdict_requires_target_stage(role: str | None, verdict: TaskActivityVerdict) -> bool:
    """
    Return whether an agent verdict must name a recovery destination.
    """
    return known_agent_role(role) is AgentRole.RECOVERY and verdict in _RECOVERY_TARGET_STAGE_VERDICTS


def agent_stage_for_task(task: TaskRecord, role: str | None = None) -> ReportPipelineState:
    """
    Pick the reportable stage label for a subagent invocation.

    The selected value is exported as LITEHIVE_STAGE and passed to
    report parsing, so it must be one of the labels StageReport accepts.
    Runtime state wins when present; role defaults are only a fallback
    for runs launched before the lifecycle mirror has been populated.
    """
    runtime_stage = _reportable_stage_from_runtime(task.current_pipeline_stage)
    if runtime_stage is not None:
        return runtime_stage

    pipeline_status_stage = _stage_from_pipeline_status(task)
    if pipeline_status_stage is not None:
        return pipeline_status_stage

    agent_role = known_agent_role(role)
    if agent_role is not None:
        return agent_role.default_stage
    return TaskStage.IMPLEMENTING


def _reportable_stage_from_runtime(current_stage: str | None) -> ReportPipelineState | None:
    """
    Translate runtime current_stage into the subset report storage accepts.

    Pipeline hooks collapse to their owning TaskStage, while recovery and
    merge resolution retain their PipelineState labels because operators
    and recovery logic need to distinguish them from ordinary grooming
    or commit work.
    """
    if current_stage is None:
        return None
    try:
        pipeline_state = PipelineState(current_stage)
    except ValueError:
        return _task_stage_from_value(current_stage)
    if pipeline_state is PipelineState.RECOVERING:
        return PipelineState.RECOVERING
    if pipeline_state is PipelineState.MERGE_RESOLVING:
        return PipelineState.MERGE_RESOLVING
    task_stage = task_stage_for_pipeline_state(pipeline_state)
    if task_stage is not None:
        return task_stage
    return None


def _task_stage_from_value(value: str) -> TaskStage | None:
    """
    Convert a direct TaskStage string without accepting arbitrary labels.
    """
    try:
        return TaskStage(value)
    except ValueError:
        return None


def _stage_from_pipeline_status(task: TaskRecord) -> TaskStage | None:
    """
    Use the task's coarse pipeline status when no runtime stage exists.
    """
    if task.pipeline_status is None:
        return None
    return _task_stage_from_value(task.pipeline_status.value)
