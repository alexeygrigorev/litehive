"""Tests for the v2 prompt serializer."""

import json
from pathlib import Path

import pytest
import yaml

from litehive.db.schema import connect_workspace_db
from litehive.domain.recovery import FailureFingerprint, RecoveryTrigger, TriggerEventKind
from litehive.roles.planner import PlannerAgent
from litehive.roles.qa import QAAgent
from litehive.roles.recovery import RecoveryAgent
from litehive.roles.reviewer import ReviewerAgent
from litehive.roles.swe import SWEAgent
from litehive.roles.base import PromptContext
from litehive.config.workspace import ensure_workspace
from litehive.config.workspace_files import config_path
from litehive.domain.lifecycle_deltas import StateDelta
from litehive.domain.reports import TaskActivityEntry
from litehive.lifecycle.events import HookOk, Pass, Reject
from litehive.lifecycle.journal import SqliteJournal
from litehive.lifecycle.persistence import LastRejection, LastReport, TaskState
from litehive.lifecycle.prompt_serializer import serialize_prompt
from litehive.lifecycle.types import PipelineMode
from litehive.state.records import create_task, save_task


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


def _instruction_layer(prompt: dict[str, object], label: str) -> str | None:
    for current_label, text in prompt.get("instruction_layers", []):
        if current_label == label:
            return text
    return None


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
    prompt = agent.build_prompt(make_state(task.id))
    text = serialize_prompt(prompt, task_record=task)

    assert prompt["instruction_variant"] == "fresh"
    assert "Instructions:" in text
    assert "## Role guidance" in text
    assert "## Fresh attempt guidance" in text
    assert "You are the SWE" in text  # from the swe.py INSTRUCTIONS
    assert "Fresh attempt: implement from the task contract" in text
    assert 'litehive report --verdict pass --role swe --message "your report text"' in text
    assert "litehive agent report" not in text
    assert "Never exit without calling `litehive report`." in text


def test_serialize_recovery_includes_recovery_trigger(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = RecoveryAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state(
        task.id,
        stage="recovering",
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
    text = serialize_prompt(agent.build_prompt(state), task_record=task)

    assert "Recovery trigger" in text
    assert "trigger_event_kind: crash" in text
    assert "origin_stage: implementing" in text
    assert "## Recovery startup guidance" in text  # the four built-in recovery bullets
    assert "You fix Litehive infrastructure bugs, not agent judgment disagreements." in text
    assert "litehive pipeline journal <task_id>" in text
    assert "litehive task logs <task_id> --agent" in text


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

    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    text = serialize_prompt(
        agent.build_prompt(make_state(task.id)),
        task_record=task,
        workspace_root=workspace,
    )

    assert "Task activity:" in text
    assert "kept" in text


def test_serialize_reads_activity_through_boundary(workspace: Path, monkeypatch) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    calls: list[tuple[Path, str]] = []

    def fake_load_activity(root: Path, task_record) -> list[TaskActivityEntry]:
        calls.append((root, task_record.id))
        return [
            TaskActivityEntry(
                role="planner",
                step="grooming",
                verdict="pass",
                message="scope ready",
            )
        ]

    monkeypatch.setattr(
        "litehive.lifecycle.prompt_serializer.load_task_activity",
        fake_load_activity,
    )

    text = serialize_prompt(
        agent.build_prompt(make_state(task.id)),
        task_record=task,
        workspace_root=workspace,
    )

    assert calls == [(workspace, task.id)]
    assert "[grooming] planner (pass): scope ready" in text


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


def test_retry_prompt_includes_prior_work_summary_from_last_report(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state(task.id)
    state.stage_retry["implementing"] = 1
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

    text = serialize_prompt(agent.build_prompt(state), task_record=task)

    assert "Prior work (last attempt):" in text
    assert (
        "- Changed files: litehive/lifecycle/prompt_serializer.py, "
        "tests/lifecycle/test_prompt_serializer.py"
    ) in text
    assert (
        "- Test results: uv run pytest -q tests/lifecycle/test_prompt_serializer.py -> 8 passed; "
        "uv run ruff check --select E402,F401 litehive tests -> all checks passed"
    ) in text


def test_retry_prompt_omits_prior_work_when_last_report_has_no_retry_summary(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state(task.id)
    state.stage_retry["implementing"] = 1

    text = serialize_prompt(agent.build_prompt(state), task_record=task)

    assert "Prior work (last attempt):" not in text


def test_retry_prompt_filters_last_rejection_reason_from_prior_work(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state(task.id)
    state.stage_retry["implementing"] = 1
    state.last_report = LastReport(
        files_changed=1,
        changed_files=["litehive/lifecycle/prompt_serializer.py"],
        test_results=[
            "uv run pytest -q tests/lifecycle/test_prompt_serializer.py -> 8 passed",
            "tests fail with ImportError",
        ],
    )
    state.last_rejection_by_stage["implementing"] = LastRejection(
        source="qa",
        reason="tests fail with ImportError",
        raised_at_phase="testing",
    )

    text = serialize_prompt(agent.build_prompt(state), task_record=task)

    assert "Prior work (last attempt):" in text
    assert "uv run pytest -q tests/lifecycle/test_prompt_serializer.py -> 8 passed" in text
    assert "- Reason: tests fail with ImportError" in text
    assert "- Test results: tests fail with ImportError" not in text


def test_swe_retry_prompt_selects_retry_attempt_guidance(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state(task.id)
    state.last_rejection_by_stage["implementing"] = LastRejection(
        source="qa",
        reason="integration sweep failed",
        raised_at_phase="testing",
    )

    prompt = agent.build_prompt(state)
    text = serialize_prompt(prompt, task_record=task)

    assert prompt["instruction_variant"] == "retry"
    assert _instruction_layer(prompt, "attempt:retry") is not None
    assert _instruction_layer(prompt, "attempt:fresh") is None
    assert "## Retry attempt guidance" in text
    assert "Retry after rejection: read the last rejection carefully" in text
    assert "Rerun the cited reproduction or verification commands exactly" in text
    assert "Fix the cited failures first" in text
    assert "Do not escape through `blocked`, stale, or environmental claims" in text
    assert "## Fresh attempt guidance" not in text
    assert "Rules when responding to a rejection:" not in text


def test_qa_prompt_includes_default_vs_opt_in_verification_guidance(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = QAAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())

    prompt = agent.build_prompt(make_state(task.id, stage="testing"))
    text = serialize_prompt(prompt, task_record=task)

    assert prompt["instruction_variant"] == "fresh"
    assert "## Fresh attempt guidance" in text
    assert "Fresh verification pass: build an independent check plan" in text
    assert "## Qa startup guidance" in text
    assert "repo's documented verification flow" in text
    assert "default deterministic test suite and targeted checks first" in text
    assert "opt-in, external-boundary, or authenticated integration coverage" in text


def test_qa_retry_prompt_selects_retry_attempt_guidance(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = QAAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state(task.id, stage="testing")
    state.last_rejection_by_stage["testing"] = LastRejection(
        source="hook",
        reason="targeted verification still fails",
        raised_at_phase="after_testing",
    )

    prompt = agent.build_prompt(state)
    text = serialize_prompt(prompt, task_record=task)

    assert prompt["instruction_variant"] == "retry"
    assert _instruction_layer(prompt, "attempt:retry") is not None
    assert _instruction_layer(prompt, "attempt:fresh") is None
    assert "## Retry attempt guidance" in text
    assert "Retry after rejection: read the last rejection carefully" in text
    assert "Verify with current evidence that the cited failures are fixed before you pass" in text
    assert "Do not escape through `blocked`, stale, or environmental claims" in text
    assert "## Fresh attempt guidance" not in text


def test_reviewer_prompt_calls_out_qa_override_with_last_testing_rejection(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = ReviewerAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state(task.id, stage="accepting")
    state.last_rejection_by_stage["accepting"] = LastRejection(
        source="agent",
        reason="qa asked for style-only cleanup",
        raised_at_phase="testing",
    )

    text = serialize_prompt(agent.build_prompt(state), task_record=task)

    assert "- Raised at phase: testing" in text
    assert "- Reason: qa asked for style-only cleanup" in text
    assert "You can override QA if the work materially meets intent — tests pass and hooks are green." in text


def test_build_prompt_ignores_corrupt_hook_config(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    config_path(workspace).write_text("[\n", encoding="utf-8")

    agent = SWEAgent(
        _NullSelector(),
        _NullSessions(),
        prompt_context=PromptContext(workspace_root=workspace),
    )
    prompt = agent.build_prompt(make_state(task.id))

    assert prompt["rejecting_hooks"] == []


def test_swe_prompt_lists_after_stage_hooks_with_descriptions(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    config_path(workspace).write_text(
        yaml.safe_dump(
            {
                "runner_hooks": {
                    "after_implementing": [
                        {
                            "command": "ruff check --select E402,F401",
                            "reject_on_failure": True,
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
        prompt_context=PromptContext(workspace_root=workspace),
    )
    text = serialize_prompt(agent.build_prompt(make_state(task.id)), task_record=task)

    assert "After implementing, these checks will run:" in text
    assert "- ruff check --select E402,F401 (ensures no unused imports or wrong import order)" in text
    assert (
        "- uv run pytest -q tests/lifecycle/test_prompt_serializer.py "
        "(runs the focused serializer regression slice)"
    ) in text
    assert "Hook failures can reject the stage." in text


def test_serialize_works_without_task_record() -> None:
    agent = PlannerAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state("T-XYZ", stage="grooming")
    text = serialize_prompt(agent.build_prompt(state), task_record=None)

    assert "Task: T-XYZ" in text
    assert "Goal:\n(task record not loaded)" in text
    assert "Acceptance criteria:\n- (none defined)" in text
    assert 'litehive report --verdict pass --role planner --message "your report text"' in text
    assert "litehive agent report" not in text


def test_serialize_verdict_instructions_match_role_and_stage(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    text = serialize_prompt(agent.build_prompt(make_state(task.id)), task_record=task)

    assert "Allowed verdicts for your role: <pass|reject>." in text


def test_recovery_prompt_uses_recovery_verdict_contract(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = RecoveryAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    state = make_state(
        task.id,
        stage="recovering",
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

    text = serialize_prompt(agent.build_prompt(state), task_record=task)

    assert "Allowed verdicts for your role: <resume|advance|done|budget_hit|reject>." in text


def test_serialize_includes_nudge_message_when_present(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    prompt = agent.build_nudge_prompt(make_state(task.id), agent.build_prompt(make_state(task.id)))

    text = serialize_prompt(prompt, task_record=task)

    assert "this is a nudge" in text
    assert "without a verdict submission" in text
    assert "Please review your work and submit your verdict now." in text
    assert "litehive report --verdict <pass|reject> --role <role>" in text


def test_implementing_retry_activity_keeps_only_grooming_and_dedups_last_rejection_by_source_and_reason(
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
    prompt["activity"] = [
        {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope " + ("x" * 600)},
        {"role": "recovery", "stage": "recovering", "verdict": "comment", "message": "bookkeeping"},
        {"role": "swe", "stage": "implementing", "verdict": "pass", "message": "old swe pass"},
        {"role": "qa", "stage": "testing", "verdict": "reject", "message": "older reject"},
        {"role": "qa", "stage": "testing", "verdict": "reject", "message": "tests fail"},
    ]

    text = serialize_prompt(prompt, task_record=task)

    activity_lines = _activity_lines(text)
    assert activity_lines == [f"[grooming] planner (pass): {'scope ' + ('x' * 482)}…(truncated)"]
    assert len(activity_lines[0].split(": ", 1)[1]) == 500
    assert "- Source: qa" in text
    assert "- Reason: tests fail" in text
    assert "bookkeeping" not in text
    assert "old swe pass" not in text
    assert "older reject" not in text


def test_implementing_prompt_uses_latest_testing_reject_that_sent_work_back(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    journal = SqliteJournal(workspace)
    journal.task_started(task.id, "ready")
    journal.transition(
        task.id,
        "after_implementing",
        Reject(source="hook", reason="old hook failure"),
        "implementing",
        "",
        StateDelta(),
    )
    journal.transition(task.id, "implementing", Pass(), "after_implementing", "", StateDelta())
    journal.transition(task.id, "after_implementing", HookOk(), "before_testing", "", StateDelta())
    journal.transition(task.id, "before_testing", HookOk(), "testing", "", StateDelta())
    journal.transition(
        task.id,
        "testing",
        Reject(source="agent", reason="latest qa failure"),
        "implementing",
        "",
        StateDelta(),
    )

    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace_root=workspace))
    state = make_state(task.id)
    state.last_rejection_by_stage["implementing"] = LastRejection(
        source="hook",
        reason="old hook failure",
        raised_at_phase="after_implementing",
    )
    state.last_rejection_by_stage["testing"] = LastRejection(
        source="agent",
        reason="latest qa failure",
        raised_at_phase="testing",
    )

    prompt = agent.build_prompt(state)

    assert prompt["last_rejection"] == {
        "source": "agent",
        "reason": "latest qa failure",
        "raised_at_phase": "testing",
    }


def test_implementing_prompt_keeps_latest_hook_reject_when_it_is_newest(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    journal = SqliteJournal(workspace)
    journal.task_started(task.id, "ready")
    journal.transition(
        task.id,
        "testing",
        Reject(source="agent", reason="older qa failure"),
        "implementing",
        "",
        StateDelta(),
    )
    journal.transition(task.id, "implementing", Pass(), "after_implementing", "", StateDelta())
    journal.transition(
        task.id,
        "after_implementing",
        Reject(source="hook", reason="newest hook failure"),
        "implementing",
        "",
        StateDelta(),
    )

    agent = SWEAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext(workspace_root=workspace))
    state = make_state(task.id)
    state.last_rejection_by_stage["implementing"] = LastRejection(
        source="hook",
        reason="newest hook failure",
        raised_at_phase="after_implementing",
    )
    state.last_rejection_by_stage["testing"] = LastRejection(
        source="agent",
        reason="older qa failure",
        raised_at_phase="testing",
    )

    prompt = agent.build_prompt(state)

    assert prompt["last_rejection"] == {
        "source": "hook",
        "reason": "newest hook failure",
        "raised_at_phase": "after_implementing",
    }


def test_activity_does_not_dedup_reject_when_source_differs(workspace: Path) -> None:
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
        "activity": [
            {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "reviewer", "stage": "accepting", "verdict": "reject", "message": "same reason"},
        ],
    }

    text = serialize_prompt(prompt, task_record=task)

    assert _activity_lines(text) == [
        "[grooming] planner (pass): scope",
        "[accepting] reviewer (reject): same reason",
    ]


def test_activity_dedups_agent_reject_when_last_rejection_uses_generic_agent_source(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = {
        "task_id": task.id,
        "stage": "custom",
        "role": "swe",
        "pipeline_mode": PipelineMode.FULL.value,
        "instruction_layers": [],
        "last_rejection": {
            "source": "agent",
            "reason": "same reason",
            "raised_at_phase": "testing",
        },
        "activity": [
            {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "qa", "stage": "testing", "verdict": "reject", "message": "same reason"},
        ],
    }

    text = serialize_prompt(prompt, task_record=task)

    assert _activity_lines(text) == [
        "[grooming] planner (pass): scope",
    ]


def test_activity_does_not_dedup_generic_agent_reject_when_stage_differs(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = {
        "task_id": task.id,
        "stage": "custom",
        "role": "swe",
        "pipeline_mode": PipelineMode.FULL.value,
        "instruction_layers": [],
        "last_rejection": {
            "source": "agent",
            "reason": "same reason",
            "raised_at_phase": "testing",
        },
        "activity": [
            {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "reviewer", "stage": "accepting", "verdict": "reject", "message": "same reason"},
        ],
    }

    text = serialize_prompt(prompt, task_record=task)

    assert _activity_lines(text) == [
        "[grooming] planner (pass): scope",
        "[accepting] reviewer (reject): same reason",
    ]


def test_activity_dedups_hook_reject_when_reason_is_embedded_in_activity_message(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = {
        "task_id": task.id,
        "stage": "custom",
        "role": "swe",
        "pipeline_mode": PipelineMode.FULL.value,
        "instruction_layers": [],
        "last_rejection": {
            "source": "hook",
            "reason": "command failed",
            "raised_at_phase": "after_implementing",
        },
        "activity": [
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
    }

    text = serialize_prompt(prompt, task_record=task)

    assert _activity_lines(text) == [
        "[grooming] planner (pass): scope",
    ]


def test_activity_section_is_omitted_when_filtering_removes_all_entries(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = {
        "task_id": task.id,
        "stage": "testing",
        "role": "qa",
        "pipeline_mode": PipelineMode.FULL.value,
        "instruction_layers": [],
        "activity": [
            {"role": "recovery", "stage": "recovering", "verdict": "comment", "message": "bookkeeping"},
        ],
    }

    text = serialize_prompt(prompt, task_record=task)

    assert "Task activity:" not in text


def test_testing_activity_keeps_only_last_implementing_pass(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = {
        "task_id": task.id,
        "stage": "testing",
        "role": "qa",
        "pipeline_mode": PipelineMode.FULL.value,
        "instruction_layers": [],
        "activity": [
            {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "swe", "stage": "implementing", "verdict": "pass", "message": "first impl"},
            {"role": "qa", "stage": "testing", "verdict": "reject", "message": "old reject"},
            {"role": "swe", "stage": "implementing", "verdict": "pass", "message": "latest impl"},
        ],
    }

    text = serialize_prompt(prompt, task_record=task)

    assert _activity_lines(text) == [
        "[implementing] swe (pass): latest impl",
    ]


def test_accepting_activity_keeps_only_last_implementing_and_testing_passes(workspace: Path) -> None:
    task = create_task(workspace, title="t", goal="g")
    prompt = {
        "task_id": task.id,
        "stage": "accepting",
        "role": "reviewer",
        "pipeline_mode": PipelineMode.FULL.value,
        "instruction_layers": [],
        "activity": [
            {"role": "planner", "stage": "grooming", "verdict": "pass", "message": "scope"},
            {"role": "swe", "stage": "implementing", "verdict": "pass", "message": "first impl"},
            {"role": "qa", "stage": "testing", "verdict": "pass", "message": "first qa"},
            {"role": "qa", "stage": "testing", "verdict": "reject", "message": "old reject"},
            {"role": "swe", "stage": "implementing", "verdict": "pass", "message": "latest impl"},
            {"role": "qa", "stage": "testing", "verdict": "pass", "message": "latest qa"},
        ],
    }

    text = serialize_prompt(prompt, task_record=task)

    assert _activity_lines(text) == [
        "[implementing] swe (pass): latest impl",
        "[testing] qa (pass): latest qa",
    ]
