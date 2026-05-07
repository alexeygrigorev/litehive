from litehive.domain.common import PipelineState, PipelineStatus, TaskStage, Verdict
from litehive.domain.roles import (
    AgentRole,
    agent_activity_verdicts_for_role,
    agent_stage_for_task,
    agent_startup_guidance_keys,
    agent_verdict_requires_target_stage,
    known_agent_role,
)
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


def test_agent_role_owns_pipeline_and_task_stage_relationships() -> None:
    assert AgentRole.PLANNER.pipeline_stages == frozenset({PipelineState.GROOMING})
    assert AgentRole.SWE.pipeline_stages == frozenset({PipelineState.IMPLEMENTING})
    assert AgentRole.QA.pipeline_stages == frozenset({PipelineState.TESTING})
    assert AgentRole.REVIEWER.pipeline_stages == frozenset({PipelineState.ACCEPTING})
    assert AgentRole.MERGE_RESOLVER.pipeline_stages == frozenset({PipelineState.MERGE_RESOLVING})
    assert AgentRole.RECOVERY.pipeline_stages == frozenset({PipelineState.RECOVERING})

    assert AgentRole.PLANNER.task_stages == frozenset({TaskStage.GROOMING})
    assert AgentRole.SWE.task_stages == frozenset({TaskStage.IMPLEMENTING})
    assert AgentRole.QA.task_stages == frozenset({TaskStage.TESTING})
    assert AgentRole.REVIEWER.task_stages == frozenset({TaskStage.ACCEPTING})
    assert AgentRole.MERGE_RESOLVER.task_stages == frozenset()
    assert AgentRole.RECOVERY.task_stages == frozenset()


def test_agent_startup_guidance_keys_are_domain_role_policy() -> None:
    assert agent_startup_guidance_keys() == frozenset({"all", "planner", "swe", "qa", "reviewer", "recovery"})
    assert "merge-resolver" not in agent_startup_guidance_keys()


def test_known_agent_role_keeps_unknown_boundary_values_out_of_domain() -> None:
    assert known_agent_role("swe") is AgentRole.SWE
    assert known_agent_role("unknown-specialist") is None
    assert known_agent_role(None) is None


def test_agent_activity_verdicts_keep_role_policy_in_domain() -> None:
    assert agent_activity_verdicts_for_role("swe") == frozenset({Verdict.PASS, Verdict.REJECT})
    assert agent_activity_verdicts_for_role("unknown-specialist") == frozenset({Verdict.PASS, Verdict.REJECT})
    assert agent_activity_verdicts_for_role("recovery") == frozenset(
        {
            Verdict.RESUME,
            Verdict.ADVANCE,
            Verdict.DONE,
            Verdict.BUDGET_HIT,
            Verdict.REJECT,
        }
    )


def test_recovery_routing_verdicts_require_target_stage() -> None:
    assert agent_verdict_requires_target_stage("recovery", Verdict.RESUME)
    assert agent_verdict_requires_target_stage("recovery", Verdict.ADVANCE)
    assert not agent_verdict_requires_target_stage("recovery", Verdict.DONE)
    assert not agent_verdict_requires_target_stage("swe", Verdict.RESUME)


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
