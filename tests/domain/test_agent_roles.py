from litehive.domain.common import PipelineState, PipelineStatus, TaskStage
from litehive.domain.roles import AgentRole, agent_stage_for_task, known_agent_role
from litehive.domain.task import TaskRecord


def _task() -> TaskRecord:
    return TaskRecord(id="T-0001", slug="sample", title="Sample")


def test_agent_role_owns_default_stage_relationships() -> None:
    assert AgentRole.PLANNER.default_stage is TaskStage.GROOMING
    assert AgentRole.SWE.default_stage is TaskStage.IMPLEMENTING
    assert AgentRole.QA.default_stage is TaskStage.TESTING
    assert AgentRole.REVIEWER.default_stage is TaskStage.ACCEPTING
    assert AgentRole.MERGE_RESOLVER.default_stage is PipelineState.MERGE_RESOLVING
    assert AgentRole.RECOVERY.default_stage is PipelineState.RECOVERING


def test_known_agent_role_keeps_unknown_boundary_values_out_of_domain() -> None:
    assert known_agent_role("swe") is AgentRole.SWE
    assert known_agent_role("unknown-specialist") is None
    assert known_agent_role(None) is None


def test_agent_stage_for_task_prefers_runtime_stage_over_role_default() -> None:
    task = _task()
    task.runtime.pipeline.current_stage.stage = PipelineState.AFTER_TESTING.value

    assert agent_stage_for_task(task, role="planner") is TaskStage.TESTING


def test_agent_stage_for_task_keeps_recovery_and_merge_pseudo_stages() -> None:
    task = _task()
    task.runtime.pipeline.current_stage.stage = PipelineState.RECOVERING.value
    assert agent_stage_for_task(task, role="swe") is PipelineState.RECOVERING

    task.runtime.pipeline.current_stage.stage = PipelineState.MERGE_RESOLVING.value
    assert agent_stage_for_task(task, role="swe") is PipelineState.MERGE_RESOLVING


def test_agent_stage_for_task_falls_back_to_pipeline_status_then_role() -> None:
    task = _task()
    task.pipeline_status = PipelineStatus.ACCEPTING

    assert agent_stage_for_task(task, role="swe") is TaskStage.ACCEPTING

    task.pipeline_status = PipelineStatus.BACKLOG
    assert agent_stage_for_task(task, role="recovery") is PipelineState.RECOVERING
    assert agent_stage_for_task(task, role="unknown-specialist") is TaskStage.IMPLEMENTING
