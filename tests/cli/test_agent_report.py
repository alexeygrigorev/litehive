from pathlib import Path

import pytest
from typer.testing import CliRunner

from litehive.agents.session_store import save_subagent_artifacts
from litehive.cli.agent_cli import agent_app
from litehive.cli.app import app as root_app
from litehive.config.workspace import ensure_workspace
from litehive.lifecycle.persistence import SqlitePersistence, TaskState
from litehive.lifecycle.types import PipelineMode
from litehive.domain.reports import SEMANTIC_REJECT_CLASSIFICATION, TaskActivityEntry
from litehive.domain.task import TaskRecord
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task
from litehive.state.records import get_task_record
from litehive.state.store import runtime_store
from litehive.tasks.activity import load_task_activity


@pytest.fixture(autouse=True)
def _workspace_root_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LITEHIVE_WORKSPACE_ROOT", str(tmp_path))


def _assert_activity_entries(
    actual: list[TaskActivityEntry],
    expected: list[TaskActivityEntry],
) -> None:
    assert len(actual) == len(expected)
    for actual_entry, expected_entry in zip(actual, expected, strict=True):
        actual_payload = actual_entry.model_dump(exclude={"created_at"})
        expected_payload = expected_entry.model_dump(exclude={"created_at"})
        if expected_entry.source_subagent_id is None:
            actual_payload.pop("source_subagent_id", None)
            expected_payload.pop("source_subagent_id", None)
        assert actual_payload == expected_payload
        assert actual_entry.created_at


def _bind_report_identity(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    task_id: str,
    role: str,
    subagent_id: str = "SA-0001",
) -> None:
    save_subagent_artifacts(
        root,
        task_id,
        subagent_id,
        session={
            "id": subagent_id,
            "role": role,
            "engine": "codex",
            "status": "running",
        },
    )
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", role)
    monkeypatch.setenv("LITEHIVE_SUBAGENT_ID", subagent_id)


def _save_intent_only_task(root: Path) -> None:
    (root / ".litehive" / "tasks" / "T-0001-missing-runtime").mkdir(parents=True, exist_ok=True)
    runtime_store(root).save_task_intent(
        "T-0001",
        TaskRecord(
            id="T-0001",
            slug="missing-runtime",
            title="Missing runtime row",
            pipeline_mode="full",
            priority="medium",
            git={
                "auto_commit": True,
                "commit_message": "missing runtime row",
            },
        ).to_intent_record(),
    )


def test_agent_report_derives_role_from_subagent_session_and_records_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Session-bound report")
    _bind_report_identity(tmp_path, monkeypatch, task_id=task.id, role="qa", subagent_id="SA-0042")
    monkeypatch.setenv("LITEHIVE_STAGE", "testing")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "pass",
            "--message",
            "verified",
            "--task-id",
            task.id,
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert "role: qa" in result.output
    assert "source_subagent_id: SA-0042" in result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    _assert_activity_entries(
        load_task_activity(tmp_path, updated),
        [
            TaskActivityEntry(
                role="qa",
                stage="testing",
                verdict="pass",
                message="verified",
                files_changed=[],
                source_subagent_id="SA-0042",
            )
        ],
    )


def test_agent_report_rejects_legacy_role_override_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="No role spoofing")
    _bind_report_identity(tmp_path, monkeypatch, task_id=task.id, role="qa")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "pass",
            "--message",
            "trying to spoof",
            "--role",
            "swe",
            "--task-id",
            task.id,
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 1
    assert "--role cannot override subagent session identity" in result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    assert load_task_activity(tmp_path, updated) == []


def test_agent_report_help_does_not_advertise_role_flag() -> None:
    result = CliRunner().invoke(agent_app, ["report", "--help"], standalone_mode=False)

    assert result.exit_code == 0
    assert "--role" not in result.output


def test_agent_report_uses_intent_record_when_runtime_row_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    _save_intent_only_task(tmp_path)
    _bind_report_identity(tmp_path, monkeypatch, task_id="T-0001", role="recovery")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "resume",
            "--message",
            "recovery completed",
            "--stage",
            "grooming",
            "--target-stage",
            "grooming",
            "--task-id",
            "T-0001",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    task = get_task_record(tmp_path, "T-0001")
    assert task is not None
    activity_entries = load_task_activity(tmp_path, task)
    _assert_activity_entries(
        activity_entries,
        [
            TaskActivityEntry(
                role="recovery",
                stage="grooming",
                target_stage="grooming",
                verdict="resume",
                message="recovery completed",
                files_changed=[],
            )
        ],
    )


def test_agent_report_persists_hidden_recovery_target_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Persist target stage")
    _bind_report_identity(tmp_path, monkeypatch, task_id=task.id, role="recovery")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "resume",
            "--message",
            "retry implementing",
            "--stage",
            "recovering",
            "--target-stage",
            "implementing",
            "--task-id",
            task.id,
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    task = get_task_record(tmp_path, task.id)
    assert task is not None
    activity_entries = load_task_activity(tmp_path, task)
    _assert_activity_entries(
        activity_entries,
        [
            TaskActivityEntry(
                role="recovery",
                stage="recovering",
                target_stage="implementing",
                verdict="resume",
                message="retry implementing",
                files_changed=[],
            )
        ],
    )


def test_agent_report_rejects_recovery_resume_without_target_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery target stage required")
    _bind_report_identity(tmp_path, monkeypatch, task_id=task.id, role="recovery")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "resume",
            "--message",
            "retry implementing",
            "--stage",
            "recovering",
            "--task-id",
            task.id,
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 1
    assert "requires --target-stage" in result.output


def test_agent_report_uses_env_stage_when_runtime_row_is_missing(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    _save_intent_only_task(tmp_path)
    _bind_report_identity(tmp_path, monkeypatch, task_id="T-0001", role="planner")
    monkeypatch.setenv("LITEHIVE_STAGE", "grooming")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "pass",
            "--message",
            "planner completed",
            "--task-id",
            "T-0001",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    task = get_task_record(tmp_path, "T-0001")
    assert task is not None
    activity_entries = load_task_activity(tmp_path, task)
    _assert_activity_entries(
        activity_entries,
        [
            TaskActivityEntry(
                role="planner",
                stage="grooming",
                verdict="pass",
                message="planner completed",
                files_changed=[],
            )
        ],
    )


def test_agent_report_prefers_env_stage_over_stale_pipeline_stage(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Prefer env stage")
    SqlitePersistence(tmp_path).save(TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL))
    _bind_report_identity(tmp_path, monkeypatch, task_id=task.id, role="planner")
    monkeypatch.setenv("LITEHIVE_STAGE", "grooming")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "reject",
            "--message",
            "planner rejected",
            "--task-id",
            task.id,
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    task = get_task_record(tmp_path, task.id)
    assert task is not None
    activity_entries = load_task_activity(tmp_path, task)
    _assert_activity_entries(
        activity_entries,
        [
            TaskActivityEntry(
                role="planner",
                stage="grooming",
                verdict="reject",
                message="planner rejected",
                files_changed=[],
            )
        ],
    )


@pytest.mark.parametrize(
    ("role", "stage"),
    [
        ("qa", "testing"),
        ("reviewer", "accepting"),
    ],
)
def test_agent_report_classifies_qa_reviewer_rejects_as_semantic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    stage: str,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title=f"{role} semantic reject")
    _bind_report_identity(tmp_path, monkeypatch, task_id=task.id, role=role)

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "reject",
            "--message",
            "acceptance evidence is incomplete",
            "--stage",
            stage,
            "--task-id",
            task.id,
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert f"verdict_classification: {SEMANTIC_REJECT_CLASSIFICATION}" in result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    activity_entries = load_task_activity(tmp_path, updated)
    assert len(activity_entries) == 1
    assert activity_entries[0].verdict == "reject"
    assert activity_entries[0].verdict_classification == SEMANTIC_REJECT_CLASSIFICATION


def test_agent_report_rejects_agent_blocked_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent blocked verdict rejected")
    _bind_report_identity(tmp_path, monkeypatch, task_id=task.id, role="swe")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "blocked",
            "--message",
            "cannot proceed",
            "--stage",
            "implementing",
            "--task-id",
            task.id,
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 1
    assert "not authorized" in result.output


def test_agent_report_rejects_removed_fail_verdict_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Legacy fail verdict")
    _bind_report_identity(tmp_path, monkeypatch, task_id=task.id, role="swe")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "fail",
            "--message",
            "legacy failure wording",
            "--stage",
            "implementing",
            "--task-id",
            task.id,
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 1
    assert "not authorized" in result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    activity_entries = load_task_activity(tmp_path, updated)
    assert activity_entries == []


def test_agent_update_allows_planner_to_shape_active_task(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Shape active task", goal="old goal")
    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "planner")
    monkeypatch.setenv("LITEHIVE_TASK_ID", task.id)

    result = CliRunner().invoke(
        agent_app,
        [
            "update",
            "--task-id",
            task.id,
            "--goal",
            "new goal",
            "--acceptance-criteria",
            "one boundary",
            "--plan-step",
            "route prompt reads through activity service",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    assert updated.goal == "new goal"
    assert updated.acceptance_criteria == ["one boundary"]
    assert updated.plan == ["route prompt reads through activity service"]


def test_root_task_update_allows_planner_agent_api(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Shape via task update", goal="old goal")
    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "planner")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        root_app,
        [
            "task",
            "update",
            task.id,
            "--workspace",
            str(tmp_path),
            "--goal",
            "new goal",
            "--acceptance-criteria",
            "one boundary",
            "--plan-step",
            "route prompt reads through activity service",
            "--constraint",
            "keep the CLI surface stable",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    assert updated.goal == "new goal"
    assert updated.acceptance_criteria == ["one boundary"]
    assert updated.plan == ["route prompt reads through activity service"]
    assert updated.constraints == ["keep the CLI surface stable"]


def test_root_task_update_rejects_non_planner_reviewer_agent(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Blocked agent task update", goal="old goal")
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        root_app,
        ["task", "update", task.id, "--workspace", str(tmp_path), "--goal", "new goal"],
        standalone_mode=False,
    )

    assert result.exit_code == 1
    assert "not authorized" in result.output


def test_root_task_close_allows_reviewer_agent_api(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Close via task group")
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "reviewer")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        root_app,
        [
            "task",
            "close",
            task.id,
            "--workspace",
            str(tmp_path),
            "--outcome",
            "duplicate",
            "--reason",
            "already tracked elsewhere",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    assert updated.status == "closed"
    assert updated.close_reason == "duplicate"
    assert updated.runtime.pipeline.last_outcome is not None
    assert updated.runtime.pipeline.last_outcome.reason == "already tracked elsewhere"


def test_root_task_close_allows_planner_to_mark_done_from_grooming(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Already satisfied on main")
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "planner")
    monkeypatch.setenv("LITEHIVE_STAGE", "grooming")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        root_app,
        [
            "task",
            "close",
            task.id,
            "--workspace",
            str(tmp_path),
            "--outcome",
            "done",
            "--reason",
            "verified current main already satisfies the request",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert "status: done" in result.output
    assert "pipeline_stage: done" in result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    assert updated.status == "done"
    assert updated.pipeline_status == "done"
    assert updated.runtime.pipeline.execution_status == "done"
    assert updated.runtime.pipeline.last_outcome is not None
    assert updated.runtime.pipeline.last_outcome.kind == "done"
    assert updated.runtime.pipeline.last_outcome.reason_code == "done"
    assert updated.runtime.pipeline.last_outcome.reason == "verified current main already satisfies the request"


def test_agent_report_rejects_legacy_recovery_pass_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery verdict contract")
    _bind_report_identity(tmp_path, monkeypatch, task_id=task.id, role="recovery")
    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "pass",
            "--message",
            "fixed it",
            "--task-id",
            task.id,
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 1
    assert "not authorized" in result.output


def test_agent_report_accepts_recovery_resume_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery resume verdict")
    _bind_report_identity(tmp_path, monkeypatch, task_id=task.id, role="recovery")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "resume",
            "--message",
            "fixed the runner; retry grooming",
            "--stage",
            "recovering",
            "--target-stage",
            "grooming",
            "--task-id",
            task.id,
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    _assert_activity_entries(
        load_task_activity(tmp_path, updated),
        [
            TaskActivityEntry(
                role="recovery",
                stage="recovering",
                target_stage="grooming",
                verdict="resume",
                message="fixed the runner; retry grooming",
                files_changed=[],
            )
        ],
    )


def test_agent_report_rejects_removed_step_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent report step alias")
    _bind_report_identity(tmp_path, monkeypatch, task_id=task.id, role="swe")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "pass",
            "--message",
            "integration report from codex",
            "--step",
            "implementing",
            "--task-id",
            task.id,
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 1
    assert result.exception is not None
    assert "No such option: --step" in str(result.exception)
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    assert load_task_activity(tmp_path, updated) == []


def test_root_report_rejects_removed_step_alias(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Legacy step alias for root report")
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)

    result = CliRunner().invoke(
        root_app,
        [
            "report",
            "--task-id",
            task.id,
            "--role",
            "recovery",
            "--verdict",
            "pass",
            "--step",
            "recovering",
            "--message",
            "recovery note",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 1
    assert result.exception is not None
    assert "No such option: --step" in str(result.exception)
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    assert load_task_activity(tmp_path, updated) == []


def test_root_report_rejects_removed_fail_verdict_alias(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Legacy root fail verdict")
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)

    result = CliRunner().invoke(
        root_app,
        [
            "report",
            "--task-id",
            task.id,
            "--role",
            "swe",
            "--verdict",
            "fail",
            "--stage",
            "implementing",
            "--message",
            "legacy root failure wording",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 1
    assert result.exception is not None
    assert "'fail' is not one of" in str(result.exception)
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    assert load_task_activity(tmp_path, updated) == []


def test_root_report_defaults_to_litehive_task_id_env(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Root report task env")
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.setenv("LITEHIVE_TASK_ID", task.id)

    result = CliRunner().invoke(
        root_app,
        [
            "report",
            "--role",
            "swe",
            "--verdict",
            "pass",
            "--message",
            "root env task id",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    activity_entries = load_task_activity(tmp_path, updated)
    assert len(activity_entries) == 1
    assert activity_entries[0].message == "root env task id"


def test_root_report_accepts_workspace_override(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Root report workspace override")
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)

    outside = tmp_path / "outside"
    outside.mkdir()
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(outside)):
        result = runner.invoke(
            root_app,
            [
                "report",
                "--workspace",
                str(tmp_path),
                "--task-id",
                task.id,
                "--role",
                "swe",
                "--verdict",
                "pass",
                "--message",
                "root workspace override",
            ],
            standalone_mode=False,
        )

    assert result.exit_code == 0, result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    activity_entries = load_task_activity(tmp_path, updated)
    assert len(activity_entries) == 1
    assert activity_entries[0].message == "root workspace override"


def test_root_report_fails_clearly_when_workspace_cannot_be_resolved(tmp_path: Path, monkeypatch) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(outside)):
        result = runner.invoke(
            root_app,
            [
                "report",
                "--role",
                "swe",
                "--verdict",
                "reject",
                "--message",
                "cannot resolve",
            ],
            standalone_mode=False,
        )

    assert result.exit_code == 0
    assert result.return_value == 1
    assert "report failed: unable to resolve workspace" in result.output


def test_agent_report_accepts_workspace_override(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent report workspace override")
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)
    _bind_report_identity(tmp_path, monkeypatch, task_id=task.id, role="swe")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--workspace",
            str(tmp_path),
            "--task-id",
            task.id,
            "--stage",
            "implementing",
            "--verdict",
            "pass",
            "--message",
            "agent workspace override",
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    activity_entries = load_task_activity(tmp_path, updated)
    assert len(activity_entries) == 1
    assert activity_entries[0].message == "agent workspace override"
