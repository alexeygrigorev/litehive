"""Tests for the prompt serializer."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from litehive.agents.session_store import SubagentArtifactPayload, subagent_artifacts
from litehive.db.schema import connect_workspace_db
from litehive.domain.common import PipelineState
from litehive.domain.recovery import FailureFingerprint, RecoveryTrigger, TriggerEventKind
from litehive.domain.runtime import RuntimeRecoveryOutcome, SubagentRef
from litehive.roles.planner import PlannerAgent
from litehive.roles.qa import QAAgent
from litehive.roles.recovery import RecoveryAgent
from litehive.roles.reviewer import ReviewerAgent
from litehive.roles.swe import SWEAgent
from litehive.roles.base import PromptContext
from litehive.config.loading import load_config_for_workspace
from litehive.config.workspace import create_workspace
from litehive.config.workspace_files import config_path
from litehive.domain.lifecycle_deltas import StateDelta
from litehive.domain.reports import SEMANTIC_REJECT_CLASSIFICATION, TaskActivityEntry
from litehive.lifecycle.events import HookOk, Pass, Reject
from litehive.lifecycle.journal import SqliteJournal
from litehive.workspace import Workspace
from litehive.lifecycle.persistence import FailedRunRecord, LastRejection, LastReport, TaskState
from litehive.lifecycle.prompt_serializer import serialize_prompt
from litehive.lifecycle.prompt_types import AgentPrompt
from litehive.lifecycle.types import FailedReason, PipelineMode
from litehive.state.records import create_task, save_task
from litehive.tasks.paths import task_dir


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    create_workspace(tmp_path)
    return tmp_path


class _NullSelector:
    def select(self, state, node_name, excluded):
        return None


class _NullSessions:
    def get_or_create(self, task_id, node_name, engine_name):
        return type("S", (), {"engine_session_id": None})()

    def persist(self, task_id, node_name, engine_name, session):
        pass


def make_state(task_id: str, stage: str = "implementing", **overrides) -> TaskState:
    return TaskState(
        task_id=task_id,
        stage=stage,  # type: ignore[arg-type]
        pipeline_mode=PipelineMode.FULL,
        **overrides,
    )


def make_prompt(
    task_id: str,
    *,
    stage: str = "custom",
    role: str = "swe",
    pipeline_mode: PipelineMode = PipelineMode.FULL,
    instruction_layers=None,
    last_rejection=None,
    activity=None,
    last_report=None,
    failed_run_history=None,
    runner_hooks=None,
) -> AgentPrompt:
    """Build an ``AgentPrompt`` from the trimmed dict shape these tests historically used."""
    del task_id  # task identity is sourced from the TaskRecord passed to serialize_prompt
    return AgentPrompt(
        role=role,
        stage=stage,  # type: ignore[arg-type]
        pipeline_mode=pipeline_mode,
        stage_retry=0,
        instruction_variant="fresh",
        instruction_layers=list(instruction_layers or []),
        last_report=last_report or {},
        last_rejection=last_rejection,
        failed_run_history=list(failed_run_history or []),
        runner_hooks=list(runner_hooks or []),
        activity=activity,
    )


def _activity_lines(text: str) -> list[str]:
    marker = "Task activity:\n"
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


def _instruction_layer(prompt, label: str) -> str | None:
    for current_label, text in prompt.instruction_layers or []:
        if current_label == label:
            return text
    return None


def test_role_agent_rejects_stage_role_mismatch(workspace: Path) -> None:
    class BadSWEAgent(SWEAgent):
        NODE_NAME = PipelineState.TESTING

    with pytest.raises(TypeError, match="does not match role swe"):
        BadSWEAgent(
            _NullSelector(),
            _NullSessions(),
            prompt_context=PromptContext(workspace=Workspace.from_path(workspace)),
        )


def test_serialize_includes_header_goal_acceptance_plan(workspace: Path) -> None:
    task = create_task(
        workspace,
        title="Add prompt serializer",
        goal="Build the serializer",
        acceptance_criteria=["Serializer exists", "Tests pass"],
    )
    task.plan = ["Read current prompt", "Write serializer module", "Cover with tests"]
    save_task(workspace, task)

    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id)
    prompt = agent.build_prompt(state)
    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert prompt.stage is PipelineState.IMPLEMENTING
    assert text.startswith("You are the SWE.")
    # task.id is a Litehive-internal identifier and should not leak into the agent prompt.
    assert task.id not in text
    assert "Task: Add prompt serializer" in text
    assert "Stage: implementing" in text
    # Role identity is now the opening sentence; the legacy "Role: swe" header line is gone.
    assert "Role: swe" not in text
    assert "Goal:\nBuild the serializer" in text
    assert "- Serializer exists" in text
    assert "- Read current prompt" in text
    assert "Constraints:\n- Keep changes scoped to the task." in text


def test_serialize_includes_role_instructions(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    prompt = agent.build_prompt(make_state(task.id))
    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert prompt.instruction_variant == "fresh"
    assert "Instructions:" in text
    assert "## Role guidance" in text
    assert "## Fresh attempt guidance" in text
    assert "You are the SWE" in text  # from the swe.py INSTRUCTIONS
    assert "Fresh attempt: implement from the task contract" in text
    assert 'litehive agent report --verdict pass --message "your report text"' in text
    assert "litehive report --verdict" not in text
    assert "Never exit without calling `litehive agent report`." in text


def test_serialize_recovery_includes_recovery_trigger(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = RecoveryAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(
        task.id,
        stage=PipelineState.RECOVERING,
        active_recovery_trigger=RecoveryTrigger(
            origin_stage="implementing",
            trigger_event_kind=TriggerEventKind.CRASH,
            failure_fingerprint=FailureFingerprint(
                fingerprint="AllEnginesExhausted",
                classification="engine_exhausted",
            ),
            reason_code="all_engines_exhausted",
            message="AllEnginesExhausted",
        ),
    )
    text = serialize_prompt(agent.build_prompt(state), task_record=task, workspace=Workspace.from_path(workspace))

    trigger = agent.build_prompt(state).recovery_trigger
    assert trigger is not None
    assert trigger["origin_stage"] == "implementing"
    assert trigger["trigger_event_kind"] == "crash"
    assert trigger["failure_fingerprint"] == {
        "fingerprint": "AllEnginesExhausted",
        "classification": "engine_exhausted",
        "diagnostics": {},
    }
    assert trigger["reason_code"] == "all_engines_exhausted"
    assert trigger["message"] == "AllEnginesExhausted"
    assert "Recovery trigger" in text
    assert "trigger_event_kind: crash" in text
    assert "origin_stage: implementing" in text
    assert "RecoveryContext" not in text
    assert "RecoveryRecord" not in text
    assert "FailureDiagnostics" not in text
    assert "litehive task evidence <task_id>" in text
    assert "litehive pipeline journal <task_id>" in text
    assert "litehive task logs <task_id> --agent" not in text


def test_serialize_recovery_diagnoses_invalid_current_config(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    config_path(workspace).write_text("- not\n- mapping\n", encoding="utf-8")
    agent = RecoveryAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(
        task.id,
        stage=PipelineState.RECOVERING,
        active_recovery_trigger=RecoveryTrigger(
            origin_stage="implementing",
            trigger_event_kind=TriggerEventKind.CRASH,
            failure_fingerprint=FailureFingerprint(
                fingerprint="boom",
                classification="runtime_error",
            ),
            reason_code="runtime_error",
            message="boom",
        ),
    )

    prompt = agent.build_prompt(state)
    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert prompt.litehive_source_path is None
    assert prompt.recovery_execution_root == str(workspace)
    assert prompt.recovery_config_diagnostic == {
        "kind": "workspace_config_load_failed",
        "config_root": str(workspace),
        "exception_type": "ValueError",
        "message": f"Config file must contain a mapping: {config_path(workspace)}",
    }
    assert "config_diagnostic:" in text
    assert "kind: workspace_config_load_failed" in text
    assert "exception_type: ValueError" in text
    assert f"Config file must contain a mapping: {config_path(workspace)}" in text


def test_serialize_recovery_inlines_failed_subagent_diagnostics(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    task.subagents.append(
        SubagentRef(id="SA-0001", role="swe", engine="codex", status="failed", path="subagents/SA-0001-swe")
    )
    save_task(workspace, task)

    subagent_base = task_dir(workspace, task) / "subagents" / "SA-0001-swe"
    subagent_base.mkdir(parents=True)
    (subagent_base / "stdout.txt").write_text(
        "litehive agent report --verdict pass failed\nattempting litehive agent report\n",
        encoding="utf-8",
    )
    (subagent_base / "stderr.txt").write_text("report failed: unable to resolve workspace\n", encoding="utf-8")
    subagent_artifacts(Workspace.from_path(workspace), task.id, "SA-0001").save(
        session=SubagentArtifactPayload({"id": "SA-0001", "status": "failed", "exit_code": 17}),
        report=SubagentArtifactPayload({
            "status": "failed",
            "summary": "implementing rejected: agent did not submit verdict via litehive agent report CLI",
        }),
    )

    agent = RecoveryAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(
        task.id,
        stage=PipelineState.RECOVERING,
        active_recovery_trigger=RecoveryTrigger(
            origin_stage="implementing",
            trigger_event_kind=TriggerEventKind.CRASH,
            failure_fingerprint=FailureFingerprint(
                fingerprint="report failed",
                classification="report_failure",
            ),
            reason_code="stage_exception",
            message="report failed",
        ),
    )

    prompt = agent.build_prompt(state)
    diagnostics = prompt.failed_subagent_diagnostics
    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert diagnostics is not None
    assert diagnostics.subagent_id == "SA-0001"
    assert diagnostics.role == "swe"
    assert diagnostics.engine == "codex"
    assert diagnostics.status == "failed"
    assert diagnostics.exit_code == 17
    assert diagnostics.did_produce_output is True
    assert diagnostics.report_summary == "implementing rejected: agent did not submit verdict via litehive agent report CLI"

    assert "Failed subagent evidence" in text
    assert "exit_code: 17" in text
    assert "did_produce_output: yes" in text
    assert "report_summary: implementing rejected: agent did not submit verdict via litehive agent report CLI" in text
    assert "output_signal: report failed: unable to resolve workspace" in text
    assert "execution trace (derived from events/artifacts)" not in text
    assert "stdout.txt" not in text
    assert "stderr.txt" not in text
    assert "unable to resolve workspace" in text


def test_serialize_recovery_includes_repeated_fingerprint_escalation(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    task.runtime.pipeline.recovery_history = [
        RuntimeRecoveryOutcome(
            origin_stage="implementing",
            trigger_event_kind="crash",
            fingerprint="RuntimeError:boom",
            classification="RuntimeError",
            budget_key="implementing:RuntimeError",
            recovery_verdict="resume",
            disposition="resumed",
            created_at="2026-04-20T00:00:00+00:00",
        )
    ]
    save_task(workspace, task)
    agent = RecoveryAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(
        task.id,
        stage=PipelineState.RECOVERING,
        active_recovery_trigger=RecoveryTrigger(
            origin_stage="implementing",
            trigger_event_kind=TriggerEventKind.CRASH,
            failure_fingerprint=FailureFingerprint(
                fingerprint="RuntimeError:boom",
                classification="RuntimeError",
            ),
            reason_code="runtime_error",
            message="RuntimeError:boom",
        ),
    )

    prompt = agent.build_prompt(state)
    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert prompt.repeated_recovery_fingerprint is not None
    assert prompt.repeated_recovery_fingerprint["count"] == 2
    assert "Recovery history" in text
    assert "RuntimeError:boom" in text
    assert "RecoveryContext" not in text
    assert "RecoveryRecord" not in text
    assert "Repeated recovery fingerprint detected" in text
    assert "Escalation required: do not resume or advance again" in text
    assert "--follow-up-task <task-id>" in text


def test_serialize_recovery_ignores_same_budget_key_with_different_fingerprint(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    task.runtime.pipeline.recovery_history = [
        RuntimeRecoveryOutcome(
            origin_stage="implementing",
            trigger_event_kind="crash",
            fingerprint="RuntimeError:boom-1",
            classification="RuntimeError",
            budget_key="implementing:RuntimeError",
            recovery_verdict="resume",
            disposition="resumed",
            created_at="2026-04-20T00:00:00+00:00",
        )
    ]
    save_task(workspace, task)
    agent = RecoveryAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(
        task.id,
        stage=PipelineState.RECOVERING,
        active_recovery_trigger=RecoveryTrigger(
            origin_stage="implementing",
            trigger_event_kind=TriggerEventKind.CRASH,
            failure_fingerprint=FailureFingerprint(
                fingerprint="RuntimeError:boom-2",
                classification="RuntimeError",
            ),
            reason_code="runtime_error",
            message="RuntimeError:boom-2",
        ),
    )

    prompt = agent.build_prompt(state)
    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert prompt.repeated_recovery_fingerprint is None
    assert "Repeated recovery fingerprint detected" not in text


def test_serialize_ignores_corrupt_task_activity_payload(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    with connect_workspace_db(workspace) as connection:
        connection.execute(
            """
            INSERT INTO task_activity (task_id, entry_index, created_at, payload)
            VALUES (?, ?, ?, ?)
            """,
            (task.id, 0, "2026-01-01T00:00:00+00:00", "{not-json"),
        )
        connection.execute(
            """
            INSERT INTO task_activity (task_id, entry_index, created_at, payload)
            VALUES (?, ?, ?, ?)
            """,
            (
                task.id,
                1,
                "2026-01-01T00:00:01+00:00",
                json.dumps(
                    {
                        "role": "planner",
                        "stage": "grooming",
                        "verdict": "pass",
                        "message": "kept",
                    }
                ),
            ),
        )

    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    text = serialize_prompt(
        agent.build_prompt(make_state(task.id)),
        task_record=task,
        workspace=Workspace.from_path(workspace),
    )

    assert "Task activity:" in text
    assert "kept" in text


def test_serialize_reads_activity_through_boundary(workspace: Path, monkeypatch) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    calls: list[tuple[Workspace, str]] = []

    def fake_load_activity(activity_log) -> list[TaskActivityEntry]:
        calls.append((activity_log.workspace, activity_log.task.id))
        return [
            TaskActivityEntry(
                role="planner",
                stage=PipelineState.GROOMING,
                verdict="pass",
                message="scope ready",
            )
        ]

    monkeypatch.setattr("litehive.tasks.activity.TaskActivityLog.load", fake_load_activity)

    text = serialize_prompt(
        agent.build_prompt(make_state(task.id)),
        task_record=task,
        workspace=Workspace.from_path(workspace),
    )

    assert calls == [(Workspace.from_path(workspace), task.id)]
    assert "[grooming] planner (pass): scope ready" in text


def test_serialize_includes_last_rejection(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id)
    state.last_rejection_by_stage[PipelineState.IMPLEMENTING] = LastRejection(
        source="qa",
        reason="tests fail with ImportError",
        raised_at_phase=PipelineState.TESTING,
    )
    text = serialize_prompt(agent.build_prompt(state), task_record=task, workspace=Workspace.from_path(workspace))

    assert "Last rejection" in text
    assert "- Source: qa" in text
    assert "- Raised at phase: testing" in text
    assert "tests fail with ImportError" in text


def test_planner_prompt_surfaces_failed_run_history(workspace: Path) -> None:
    task = create_task(workspace, title="Repeated retry exhaustion")
    agent = PlannerAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id, stage=PipelineState.GROOMING)
    state.failed_run_history["implementing:agent:tests fail"] = FailedRunRecord(
        stage=PipelineState.IMPLEMENTING,
        failure_shape="agent:tests fail",
        count=2,
        latest_at="2026-04-25T10:00:00+00:00",
        last_reason="tests fail",
        source="agent",
        retry_limit=3,
        failed_reason=FailedReason.SEMANTIC_REJECT,
    )

    prompt = agent.build_prompt(state)
    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert prompt.failed_run_history[0]["stage"] == "implementing"
    assert prompt.failed_run_history[0]["count"] == 2
    assert "Failed-run history (survives requeue/reset for this task):" in text
    assert "stage=implementing shape=agent:tests fail count=2" in text
    assert "reason=tests fail" in text


def test_planner_prompt_requires_current_main_preflight_before_scoping(workspace: Path) -> None:
    task = create_task(workspace, title="Avoid redundant implementation", goal="check current main first")
    agent = PlannerAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))

    text = serialize_prompt(agent.build_prompt(make_state(task.id, stage=PipelineState.GROOMING)), task_record=task, workspace=Workspace.from_path(workspace))

    preflight_idx = text.index("run a grooming preflight against current `main` and recent landed work")
    planning_idx = text.index("Frame the real user problem")
    assert preflight_idx < planning_idx
    assert "`litehive agent close --outcome done --reason ...`" in text
    assert "`litehive agent close --outcome duplicate --reason ...`" in text
    assert "Do not use operator inspection/control commands" in text
    assert "`litehive task close <task-id>" not in text


def test_retry_prompt_includes_prior_work_summary_from_last_report(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id)
    state.stage_retry[PipelineState.IMPLEMENTING] = 1
    state.last_report = LastReport(
        files_changed=2,
        changed_files=[
            "litehive/lifecycle/prompt_serializer.py",
            "tests/lifecycle/test_prompt_serializer.py",
        ],
        test_results=[
            "uv run pytest -q tests/lifecycle/test_prompt_serializer.py -> 8 passed",
            "uv run ruff check --select E402,F401 litehive tests -> all checks passed",
        ],
    )

    text = serialize_prompt(agent.build_prompt(state), task_record=task, workspace=Workspace.from_path(workspace))

    assert "Prior work (last attempt):" in text
    assert (
        "- Changed files: litehive/lifecycle/prompt_serializer.py, tests/lifecycle/test_prompt_serializer.py"
    ) in text
    assert (
        "- Test results: uv run pytest -q tests/lifecycle/test_prompt_serializer.py -> 8 passed; "
        "uv run ruff check --select E402,F401 litehive tests -> all checks passed"
    ) in text


def test_retry_prompt_omits_prior_work_when_last_report_has_no_retry_summary(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id)
    state.stage_retry[PipelineState.IMPLEMENTING] = 1

    text = serialize_prompt(agent.build_prompt(state), task_record=task, workspace=Workspace.from_path(workspace))

    assert "Prior work (last attempt):" not in text


def test_retry_prompt_filters_last_rejection_reason_from_prior_work(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id)
    state.stage_retry[PipelineState.IMPLEMENTING] = 1
    state.last_report = LastReport(
        files_changed=1,
        changed_files=["litehive/lifecycle/prompt_serializer.py"],
        test_results=[
            "uv run pytest -q tests/lifecycle/test_prompt_serializer.py -> 8 passed",
            "tests fail with ImportError",
        ],
    )
    state.last_rejection_by_stage[PipelineState.IMPLEMENTING] = LastRejection(
        source="qa",
        reason="tests fail with ImportError",
        raised_at_phase=PipelineState.TESTING,
    )

    text = serialize_prompt(agent.build_prompt(state), task_record=task, workspace=Workspace.from_path(workspace))

    assert "Prior work (last attempt):" in text
    assert "uv run pytest -q tests/lifecycle/test_prompt_serializer.py -> 8 passed" in text
    assert "- Reason: tests fail with ImportError" in text
    assert "- Test results: tests fail with ImportError" not in text


def test_swe_retry_prompt_selects_retry_attempt_guidance(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id)
    state.last_rejection_by_stage[PipelineState.IMPLEMENTING] = LastRejection(
        source="qa",
        reason="integration sweep failed",
        raised_at_phase=PipelineState.TESTING,
    )

    prompt = agent.build_prompt(state)
    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert prompt.instruction_variant == "retry"
    assert _instruction_layer(prompt, "attempt:retry") is not None
    assert _instruction_layer(prompt, "attempt:fresh") is None
    assert "## Retry attempt guidance" in text
    assert "## Fresh attempt guidance" not in text


def test_qa_prompt_includes_default_vs_opt_in_verification_guidance(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = QAAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))

    prompt = agent.build_prompt(make_state(task.id, stage=PipelineState.TESTING))
    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert prompt.instruction_variant == "fresh"
    assert "## Fresh attempt guidance" in text
    assert "## Qa startup guidance" in text


def test_qa_retry_prompt_selects_retry_attempt_guidance(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = QAAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id, stage=PipelineState.TESTING)
    state.last_rejection_by_stage[PipelineState.TESTING] = LastRejection(
        source="hook",
        reason="targeted verification still fails",
        raised_at_phase=PipelineState.AFTER_TESTING,
    )

    prompt = agent.build_prompt(state)
    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert prompt.instruction_variant == "retry"
    assert _instruction_layer(prompt, "attempt:retry") is not None
    assert _instruction_layer(prompt, "attempt:fresh") is None
    assert "## Retry attempt guidance" in text
    assert "## Fresh attempt guidance" not in text


def test_reviewer_prompt_calls_out_qa_override_with_last_testing_rejection(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = ReviewerAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id, stage=PipelineState.ACCEPTING)
    state.last_rejection_by_stage[PipelineState.ACCEPTING] = LastRejection(
        source="agent",
        reason="qa asked for style-only cleanup",
        raised_at_phase=PipelineState.TESTING,
    )

    prompt = agent.build_prompt(state)
    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert prompt.last_rejection == {
        "source": "agent",
        "reason": "qa asked for style-only cleanup",
        "raised_at_phase": "testing",
    }
    assert "- Raised at phase: testing" in text
    assert "- Reason: qa asked for style-only cleanup" in text


def test_build_prompt_ignores_corrupt_hook_config(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    config_path(workspace).write_text("[\n", encoding="utf-8")

    agent = SWEAgent(
        _NullSelector(),
        _NullSessions(),
        prompt_context=PromptContext(workspace=Workspace.from_path(workspace)),
    )
    prompt = agent.build_prompt(make_state(task.id))

    assert prompt.runner_hooks == []
    assert not hasattr(prompt, "rejecting_hooks")


def test_swe_prompt_lists_after_stage_hooks_with_descriptions(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    config_path(workspace).write_text(
        yaml.safe_dump(
            {
                "runner_hooks": {
                    "after_implementing": [
                        {
                            "command": "ruff check --select E402,F401",
                            "description": "ensures no unused imports or wrong import order",
                        },
                        {
                            "command": "uv run pytest -q tests/lifecycle/test_prompt_serializer.py",
                            "description": "runs the focused serializer regression slice",
                        },
                    ]
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    agent = SWEAgent(
        _NullSelector(),
        _NullSessions(),
        prompt_context=PromptContext(
            workspace=Workspace.from_path(workspace),
            config=load_config_for_workspace(Workspace.from_path(workspace)),
        ),
    )
    prompt = agent.build_prompt(make_state(task.id))
    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert not hasattr(prompt, "rejecting_hooks")
    assert "After implementing, these checks will run:" in text
    assert "- ruff check --select E402,F401 (ensures no unused imports or wrong import order)" in text
    assert (
        "- uv run pytest -q tests/lifecycle/test_prompt_serializer.py (runs the focused serializer regression slice)"
    ) in text
    assert "Hook failures can reject the stage." in text


def test_serialize_works_without_task_record(workspace: Path) -> None:
    agent = PlannerAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state("T-XYZ", stage=PipelineState.GROOMING)
    text = serialize_prompt(agent.build_prompt(state), task_record=None, workspace=Workspace.from_path(workspace))

    # task identifiers are not exposed to the agent: missing-record falls back to a placeholder header.
    assert "T-XYZ" not in text
    assert "Task: (task record not loaded)" in text
    assert "Goal:\n(task record not loaded)" in text
    assert "Acceptance criteria:\n- (none defined)" in text
    assert 'litehive agent report --verdict pass --message "your report text"' in text
    assert "litehive report --verdict" not in text


def test_serialize_verdict_instructions_match_role_and_stage(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    text = serialize_prompt(agent.build_prompt(make_state(task.id)), task_record=task, workspace=Workspace.from_path(workspace))

    assert "Allowed verdicts for your role: <pass|reject>." in text


def test_recovery_prompt_uses_recovery_verdict_contract(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = RecoveryAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(
        task.id,
        stage=PipelineState.RECOVERING,
        active_recovery_trigger=RecoveryTrigger(
            origin_stage="implementing",
            trigger_event_kind=TriggerEventKind.CRASH,
            failure_fingerprint=FailureFingerprint(
                fingerprint="AllEnginesExhausted",
                classification="engine_exhausted",
            ),
            reason_code="all_engines_exhausted",
            message="AllEnginesExhausted",
        ),
    )

    text = serialize_prompt(agent.build_prompt(state), task_record=task, workspace=Workspace.from_path(workspace))

    assert "Allowed verdicts for your role: <resume|advance|done|budget_hit|reject>." in text


def test_serialize_includes_nudge_message_when_present(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    prompt = agent.build_nudge_prompt(make_state(task.id), agent.build_prompt(make_state(task.id)))

    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert "this is a nudge" in text
    assert "without a verdict submission" in text
    assert "Please review your work and submit your verdict now." in text
    assert "litehive agent report --verdict <pass|reject>" in text


def test_implementing_retry_activity_keeps_only_grooming_and_dedups_last_rejection_by_source_and_reason(
    workspace: Path,
) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id)
    state.stage_retry[PipelineState.IMPLEMENTING] = 1
    state.last_rejection_by_stage[PipelineState.IMPLEMENTING] = LastRejection(
        source="qa",
        reason="tests fail",
        raised_at_phase=PipelineState.TESTING,
    )
    prompt = replace(
        agent.build_prompt(state),
        activity=[
            {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope " + ("x" * 600)},
            {"role": "recovery", "stage": "recovering", "verdict": "comment", "message": "bookkeeping"},
            {"role": "swe", "stage": "implementing", "verdict": "pass", "message": "old swe pass"},
            {"role": "qa", "stage": "testing", "verdict": "reject", "message": "older reject"},
            {"role": "qa", "stage": "testing", "verdict": "reject", "message": "tests fail"},
        ],
    )

    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    activity_lines = _activity_lines(text)
    assert activity_lines == [f"[grooming] planner (pass): {'scope ' + ('x' * 482)}…(truncated)"]
    assert len(activity_lines[0].split(": ", 1)[1]) == 500
    assert "- Source: qa" in text
    assert "- Reason: tests fail" in text
    assert "bookkeeping" not in text
    assert "old swe pass" not in text
    assert "older reject" not in text


def test_prompt_surfaces_semantic_reject_classification(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id)
    state.stage_retry[PipelineState.IMPLEMENTING] = 1
    state.last_rejection_by_stage[PipelineState.IMPLEMENTING] = LastRejection(
        source="agent",
        reason="acceptance evidence is incomplete",
        raised_at_phase=PipelineState.ACCEPTING,
        classification=SEMANTIC_REJECT_CLASSIFICATION,
    )
    prompt = replace(
        agent.build_prompt(state),
        stage="custom",  # type: ignore[arg-type]
        activity=[
            {
                "role": "reviewer",
                "stage": "accepting",
                "verdict": "reject",
                "verdict_classification": SEMANTIC_REJECT_CLASSIFICATION,
                "message": "reviewer rejected on AC2",
            }
        ],
    )

    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert "- Classification: semantic_reject" in text
    assert "[accepting] reviewer (reject; classification=semantic_reject): reviewer rejected on AC2" in text


def test_implementing_prompt_uses_latest_testing_reject_that_sent_work_back(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    journal = SqliteJournal(Workspace.from_path(workspace))
    journal.task_started(task.id, PipelineState.READY)
    journal.transition(
        task.id,
        PipelineState.AFTER_IMPLEMENTING,
        Reject(source="hook", reason="old hook failure"),
        PipelineState.IMPLEMENTING,
        "",
        StateDelta(),
    )
    journal.transition(task.id, PipelineState.IMPLEMENTING, Pass(), PipelineState.AFTER_IMPLEMENTING, "", StateDelta())
    journal.transition(task.id, PipelineState.AFTER_IMPLEMENTING, HookOk(), PipelineState.BEFORE_TESTING, "", StateDelta())
    journal.transition(task.id, PipelineState.BEFORE_TESTING, HookOk(), PipelineState.TESTING, "", StateDelta())
    journal.transition(
        task.id,
        PipelineState.TESTING,
        Reject(source="agent", reason="latest qa failure"),
        PipelineState.IMPLEMENTING,
        "",
        StateDelta(),
    )

    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id)
    state.last_rejection_by_stage[PipelineState.IMPLEMENTING] = LastRejection(
        source="hook",
        reason="old hook failure",
        raised_at_phase=PipelineState.AFTER_IMPLEMENTING,
    )
    state.last_rejection_by_stage[PipelineState.TESTING] = LastRejection(
        source="agent",
        reason="latest qa failure",
        raised_at_phase=PipelineState.TESTING,
    )

    prompt = agent.build_prompt(state)

    assert prompt.last_rejection == {
        "source": "agent",
        "reason": "latest qa failure",
        "raised_at_phase": "testing",
    }


def test_implementing_prompt_keeps_latest_hook_reject_when_it_is_newest(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    journal = SqliteJournal(Workspace.from_path(workspace))
    journal.task_started(task.id, PipelineState.READY)
    journal.transition(
        task.id,
        PipelineState.TESTING,
        Reject(source="agent", reason="older qa failure"),
        PipelineState.IMPLEMENTING,
        "",
        StateDelta(),
    )
    journal.transition(task.id, PipelineState.IMPLEMENTING, Pass(), PipelineState.AFTER_IMPLEMENTING, "", StateDelta())
    journal.transition(
        task.id,
        PipelineState.AFTER_IMPLEMENTING,
        Reject(source="hook", reason="newest hook failure"),
        PipelineState.IMPLEMENTING,
        "",
        StateDelta(),
    )

    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace=Workspace.from_path(workspace)))
    state = make_state(task.id)
    state.last_rejection_by_stage[PipelineState.IMPLEMENTING] = LastRejection(
        source="hook",
        reason="newest hook failure",
        raised_at_phase=PipelineState.AFTER_IMPLEMENTING,
    )
    state.last_rejection_by_stage[PipelineState.TESTING] = LastRejection(
        source="agent",
        reason="older qa failure",
        raised_at_phase=PipelineState.TESTING,
    )

    prompt = agent.build_prompt(state)

    assert prompt.last_rejection == {
        "source": "hook",
        "reason": "newest hook failure",
        "raised_at_phase": "after_implementing",
    }


def test_activity_does_not_dedup_reject_when_source_differs(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = make_prompt(
        task.id,
        last_rejection={
            "source": "qa",
            "reason": "same reason",
            "raised_at_phase": "testing",
        },
        activity=[
            {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "reviewer", "stage": "accepting", "verdict": "reject", "message": "same reason"},
        ],
    )

    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert _activity_lines(text) == [
        "[grooming] planner (pass): scope",
        "[accepting] reviewer (reject): same reason",
    ]


def test_activity_dedups_agent_reject_when_last_rejection_uses_generic_agent_source(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = make_prompt(
        task.id,
        last_rejection={
            "source": "agent",
            "reason": "same reason",
            "raised_at_phase": "testing",
        },
        activity=[
            {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "qa", "stage": "testing", "verdict": "reject", "message": "same reason"},
        ],
    )

    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert _activity_lines(text) == [
        "[grooming] planner (pass): scope",
    ]


def test_activity_does_not_dedup_generic_agent_reject_when_stage_differs(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = make_prompt(
        task.id,
        last_rejection={
            "source": "agent",
            "reason": "same reason",
            "raised_at_phase": "testing",
        },
        activity=[
            {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "reviewer", "stage": "accepting", "verdict": "reject", "message": "same reason"},
        ],
    )

    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert _activity_lines(text) == [
        "[grooming] planner (pass): scope",
        "[accepting] reviewer (reject): same reason",
    ]


def test_activity_dedups_hook_reject_when_reason_is_embedded_in_activity_message(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = make_prompt(
        task.id,
        last_rejection={
            "source": "hook",
            "reason": "command failed",
            "raised_at_phase": "after_implementing",
        },
        activity=[
            {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope"},
            {
                "role": "hook",
                "stage": "implementing",
                "verdict": "reject",
                "message": (
                    "Runner hook at `after_implementing` rejected the stage.\n\n"
                    "command failed\n\n"
                    "report: .litehive/tasks/T-1/reports/implementing-001.yaml"
                ),
            },
        ],
    )

    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert _activity_lines(text) == [
        "[grooming] planner (pass): scope",
    ]


def test_activity_section_is_omitted_when_filtering_removes_all_entries(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = make_prompt(
        task.id,
        stage=PipelineState.TESTING,
        role="qa",
        activity=[
            {"role": "recovery", "stage": "recovering", "verdict": "comment", "message": "bookkeeping"},
        ],
    )

    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert "Task activity:" not in text


def test_testing_activity_keeps_only_last_implementing_pass(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = make_prompt(
        task.id,
        stage=PipelineState.TESTING,
        role="qa",
        activity=[
            {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "swe", "stage": "implementing", "verdict": "pass", "message": "first impl"},
            {"role": "qa", "stage": "testing", "verdict": "reject", "message": "old reject"},
            {"role": "swe", "stage": "implementing", "verdict": "pass", "message": "latest impl"},
        ],
    )

    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert _activity_lines(text) == [
        "[implementing] swe (pass): latest impl",
    ]


def test_accepting_activity_keeps_only_last_implementing_and_testing_passes(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = make_prompt(
        task.id,
        stage=PipelineState.ACCEPTING,
        role="reviewer",
        activity=[
            {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "swe", "stage": "implementing", "verdict": "pass", "message": "first impl"},
            {"role": "qa", "stage": "testing", "verdict": "pass", "message": "first qa"},
            {"role": "qa", "stage": "testing", "verdict": "reject", "message": "old reject"},
            {"role": "swe", "stage": "implementing", "verdict": "pass", "message": "latest impl"},
            {"role": "qa", "stage": "testing", "verdict": "pass", "message": "latest qa"},
        ],
    )

    text = serialize_prompt(prompt, task_record=task, workspace=Workspace.from_path(workspace))

    assert _activity_lines(text) == [
        "[implementing] swe (pass): latest impl",
        "[testing] qa (pass): latest qa",
    ]
