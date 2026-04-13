"""Tests for the v2 prompt serializer."""

from pathlib import Path

import pytest

from litehive.pipeline.agents.planner import PlannerAgent
from litehive.pipeline.agents.recovery import RecoveryAgent
from litehive.pipeline.agents.swe import SWEAgent
from litehive.pipeline.agents.base import PromptContext
from litehive.pipeline.persistence import LastRejection, TaskState
from litehive.pipeline.prompt_serializer import serialize_prompt
from litehive.pipeline.types import PipelineMode
from litehive.state.records import create_task, save_task

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


def _discussion_lines(text: str) -> list[str]:
    marker = "Discussion thread:\n"
    if marker not in text:
        return []
    tail = text.split(marker, 1)[1]
    end_markers = (
        "\n\nChecks that will reject your work if they fail:",
        "\n\nIMPORTANT: when you are done, submit your verdict by running:",
    )
    end = len(tail)
    for candidate in end_markers:
        idx = tail.find(candidate)
        if idx != -1:
            end = min(end, idx)
    return [line for line in tail[:end].splitlines() if line.strip()]


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


def test_implementing_retry_thread_keeps_only_grooming_and_dedups_last_rejection_by_source_and_reason(
    workspace: Path,
) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state(task.id)
    state.stage_retry["implementing"] = 1
    state.last_rejection_by_stage["implementing"] = LastRejection(
        source="qa",
        reason="tests fail",
        raised_at_phase="testing",
    )
    prompt = agent.build_prompt(state)
    prompt["thread"] = [
        {"role": "planner", "step": "grooming", "verdict": "pass", "message": "scope " + ("x" * 600)},
        {"role": "recovery", "step": "recovering", "verdict": "comment", "message": "bookkeeping"},
        {"role": "swe", "step": "implementing", "verdict": "pass", "message": "old swe pass"},
        {"role": "qa", "step": "testing", "verdict": "reject", "message": "older reject"},
        {"role": "qa", "step": "testing", "verdict": "reject", "message": "tests fail"},
    ]

    text = serialize_prompt(prompt, task_record=task)

    assert _discussion_lines(text) == [
        f"[grooming] planner (pass): {'scope ' + ('x' * 494)}…(truncated)"
    ]
    assert "- Source: qa" in text
    assert "- Reason: tests fail" in text
    assert "bookkeeping" not in text
    assert "old swe pass" not in text
    assert "older reject" not in text


def test_thread_does_not_dedup_reject_when_source_differs(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = {
        "task_id": task.id,
        "stage": "custom",
        "role": "swe",
        "pipeline_mode": PipelineMode.FULL.value,
        "instruction_layers": [],
        "last_rejection": {
            "source": "qa",
            "reason": "same reason",
            "raised_at_phase": "testing",
        },
        "thread": [
            {"role": "planner", "step": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "reviewer", "step": "accepting", "verdict": "reject", "message": "same reason"},
        ],
    }

    text = serialize_prompt(prompt, task_record=task)

    assert _discussion_lines(text) == [
        "[grooming] planner (pass): scope",
        "[accepting] reviewer (reject): same reason",
    ]


def test_testing_thread_keeps_only_last_implementing_pass(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = {
        "task_id": task.id,
        "stage": "testing",
        "role": "qa",
        "pipeline_mode": PipelineMode.FULL.value,
        "instruction_layers": [],
        "thread": [
            {"role": "planner", "step": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "swe", "step": "implementing", "verdict": "pass", "message": "first impl"},
            {"role": "qa", "step": "testing", "verdict": "reject", "message": "old reject"},
            {"role": "swe", "step": "implementing", "verdict": "pass", "message": "latest impl"},
        ],
    }

    text = serialize_prompt(prompt, task_record=task)

    assert _discussion_lines(text) == [
        "[implementing] swe (pass): latest impl",
    ]


def test_accepting_thread_keeps_only_last_implementing_and_testing_passes(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = {
        "task_id": task.id,
        "stage": "accepting",
        "role": "reviewer",
        "pipeline_mode": PipelineMode.FULL.value,
        "instruction_layers": [],
        "thread": [
            {"role": "planner", "step": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "swe", "step": "implementing", "verdict": "pass", "message": "first impl"},
            {"role": "qa", "step": "testing", "verdict": "pass", "message": "first qa"},
            {"role": "qa", "step": "testing", "verdict": "reject", "message": "old reject"},
            {"role": "swe", "step": "implementing", "verdict": "pass", "message": "latest impl"},
            {"role": "qa", "step": "testing", "verdict": "pass", "message": "latest qa"},
        ],
    }

    text = serialize_prompt(prompt, task_record=task)

    assert _discussion_lines(text) == [
        "[implementing] swe (pass): latest impl",
        "[testing] qa (pass): latest qa",
    ]
