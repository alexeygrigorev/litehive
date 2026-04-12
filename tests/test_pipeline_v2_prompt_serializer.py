"""Tests for the v2 prompt serializer."""

from pathlib import Path

import pytest

from litehive.pipeline.agents import PlannerAgent, RecoveryAgent, SWEAgent
from litehive.pipeline.agents.base import PromptContext
from litehive.pipeline.persistence import LastRejection, TaskState
from litehive.pipeline.prompt_serializer import serialize_prompt
from litehive.pipeline.types import PipelineMode
from litehive.tasks.crud import create_task, save_task

from tests.workspace_helpers import ensure_workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ensure_workspace(tmp_path)
    return tmp_path


class _NullSelector:
    def select(self, state, node_name, excluded):
        return None


class _NullSessions:
    def get_or_create(self, task_id, node_name, engine_name):
        return type("S", (), {"engine_session_id": None, "turn_count": 0, "metadata": {}})()

    def persist(self, task_id, node_name, engine_name, session):
        pass


def make_state(task_id: str, stage: str = "implementing", **overrides) -> TaskState:
    return TaskState(
        task_id=task_id,
        stage=stage,
        pipeline_mode=PipelineMode.FULL,
        **overrides,
    )


def test_serialize_includes_header_goal_acceptance_plan(workspace: Path) -> None:
    task = create_task(
        workspace,
        title="Add v2 prompt serializer",
        goal="Build the serializer",
        acceptance_criteria=["Serializer exists", "Tests pass"],
    )
    task.plan = ["Read v1 prompt", "Write v2 module", "Cover with tests"]
    save_task(workspace, task)

    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state(task.id)
    prompt = agent.build_prompt(state)
    text = serialize_prompt(prompt, task_record=task)

    assert f"Task: {task.id}" in text
    assert "Add v2 prompt serializer" in text
    assert "Stage: implementing" in text
    assert "Role: swe" in text
    assert "Goal:\nBuild the serializer" in text
    assert "- Serializer exists" in text
    assert "- Read v1 prompt" in text
    assert "Constraints:\n- Keep changes scoped to the task." in text


def test_serialize_includes_role_instructions(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    text = serialize_prompt(agent.build_prompt(make_state(task.id)), task_record=task)

    assert "Instructions:" in text
    assert "## Role guidance" in text
    assert "You are the SWE" in text  # from the swe.py INSTRUCTIONS


def test_serialize_recovery_includes_failure_context(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = RecoveryAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state(
        task.id,
        stage="recovering",
        origin_stage="implementing",
        failure_context={
            "trigger_event": "Crash",
            "source": None,
            "reason": "AllEnginesExhausted",
            "raised_at_phase": "implementing",
        },
    )
    text = serialize_prompt(agent.build_prompt(state), task_record=task)

    assert "Failure context" in text
    assert "trigger_event: Crash" in text
    assert "origin_stage: implementing" in text
    assert "## Recovery startup guidance" in text  # the four built-in recovery bullets
    assert "litehive pipeline journal <task_id>" in text
    assert "litehive task logs <task_id> --agent" in text


def test_serialize_includes_last_rejection(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state(task.id)
    state.last_rejection_by_stage["implementing"] = LastRejection(
        source="qa",
        reason="tests fail with ImportError",
        raised_at_phase="testing",
    )
    text = serialize_prompt(agent.build_prompt(state), task_record=task)

    assert "Last rejection" in text
    assert "- Source: qa" in text
    assert "- Raised at phase: testing" in text
    assert "tests fail with ImportError" in text


def test_serialize_works_without_task_record() -> None:
    agent = PlannerAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state("T-XYZ", stage="grooming")
    text = serialize_prompt(agent.build_prompt(state), task_record=None)

    assert "Task: T-XYZ" in text
    assert "Goal:\n(task record not loaded)" in text
    assert "Acceptance criteria:\n- (none defined)" in text
    assert "litehive agent report" in text


def test_serialize_verdict_instructions_match_role_and_stage(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    text = serialize_prompt(agent.build_prompt(make_state(task.id)), task_record=task)

    assert "litehive agent report --verdict <pass|reject|blocked>" in text


def test_serialize_includes_nudge_message_when_present(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    prompt = agent.build_nudge_prompt(make_state(task.id), agent.build_prompt(make_state(task.id)))

    text = serialize_prompt(prompt, task_record=task)

    assert "this is a nudge" in text
    assert "without a verdict submission" in text
    assert "Please review your work and submit your verdict now." in text
