from pathlib import Path

import yaml
from typer.testing import CliRunner

from litehive.cli.agent_cli import agent_app
from litehive.cli.app import app as root_app
from litehive.config.workspace import ensure_workspace
from litehive.lifecycle.persistence import SqlitePersistence, TaskState
from litehive.lifecycle.types import PipelineMode
from litehive.domain.reports import TaskThreadComment
from litehive.state.persist import load_state, save_state
from litehive.state.records import get_task_record
from litehive.state.records import create_task
from litehive.tasks.reports import load_task_thread


def _assert_thread_comments(
    actual: list[TaskThreadComment],
    expected: list[TaskThreadComment],
) -> None:
    assert len(actual) == len(expected)
    for actual_comment, expected_comment in zip(actual, expected, strict=True):
        assert actual_comment.model_dump(exclude={"created_at"}) == expected_comment.model_dump(
            exclude={"created_at"}
        )
        assert actual_comment.created_at


def test_agent_report_uses_intent_record_when_runtime_row_is_missing(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task_dir = tmp_path / ".litehive" / "tasks" / "T-0001-missing-runtime"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "T-0001",
                "slug": "missing-runtime",
                "title": "Missing runtime row",
                "pipeline_mode": "full",
                "priority": "medium",
                "git": {
                    "auto_commit": True,
                    "commit_message": "missing runtime row",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "resume",
            "--message",
            "recovery completed",
            "--role",
            "recovery",
            "--stage",
            "grooming",
            "--target-stage",
            "grooming",
            "--task-id",
            "T-0001",
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    task = get_task_record(tmp_path, "T-0001")
    assert task is not None
    comments = load_task_thread(tmp_path, task)
    _assert_thread_comments(comments, [
        TaskThreadComment(
            role="recovery",
            stage="grooming",
            target_stage="grooming",
            verdict="resume",
            message="recovery completed",
            files_changed=[],
        )
    ])


def test_agent_report_persists_hidden_recovery_target_stage(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Persist target stage")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "resume",
            "--message",
            "retry implementing",
            "--role",
            "recovery",
            "--stage",
            "recovering",
            "--target-stage",
            "implementing",
            "--task-id",
            task.id,
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    task = get_task_record(tmp_path, task.id)
    assert task is not None
    comments = load_task_thread(tmp_path, task)
    _assert_thread_comments(comments, [
        TaskThreadComment(
            role="recovery",
            stage="recovering",
            target_stage="implementing",
            verdict="resume",
            message="retry implementing",
            files_changed=[],
        )
    ])


def test_agent_report_rejects_recovery_resume_without_target_stage(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery target stage required")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "resume",
            "--message",
            "retry implementing",
            "--role",
            "recovery",
            "--stage",
            "recovering",
            "--task-id",
            task.id,
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 1
    assert "requires --target-stage" in result.output


def test_agent_report_uses_env_stage_when_runtime_row_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    ensure_workspace(tmp_path)
    task_dir = tmp_path / ".litehive" / "tasks" / "T-0001-missing-runtime"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "T-0001",
                "slug": "missing-runtime",
                "title": "Missing runtime row",
                "pipeline_mode": "full",
                "priority": "medium",
                "git": {
                    "auto_commit": True,
                    "commit_message": "missing runtime row",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITEHIVE_STAGE", "grooming")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "pass",
            "--message",
            "planner completed",
            "--role",
            "planner",
            "--task-id",
            "T-0001",
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    task = get_task_record(tmp_path, "T-0001")
    assert task is not None
    comments = load_task_thread(tmp_path, task)
    _assert_thread_comments(comments, [
        TaskThreadComment(
            role="planner",
            stage="grooming",
            verdict="pass",
            message="planner completed",
            files_changed=[],
        )
    ])

def test_agent_report_prefers_env_stage_over_stale_pipeline_stage(
    tmp_path: Path, monkeypatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Prefer env stage")
    SqlitePersistence(tmp_path).save(
        TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    )
    monkeypatch.setenv("LITEHIVE_STAGE", "grooming")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "blocked",
            "--message",
            "planner blocked",
            "--role",
            "planner",
            "--task-id",
            task.id,
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    task = get_task_record(tmp_path, task.id)
    assert task is not None
    comments = load_task_thread(tmp_path, task)
    _assert_thread_comments(comments, [
        TaskThreadComment(
            role="planner",
            stage="grooming",
            verdict="blocked",
            message="planner blocked",
            files_changed=[],
        )
    ])


def test_agent_update_allows_planner_to_shape_active_task(
    tmp_path: Path, monkeypatch
) -> None:
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
            "--workspace",
            str(tmp_path),
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


def test_agent_report_rejects_legacy_recovery_pass_verdict(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery verdict contract")
    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "pass",
            "--message",
            "fixed it",
            "--role",
            "recovery",
            "--task-id",
            task.id,
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 1
    assert "not authorized" in result.output


def test_agent_report_accepts_recovery_resume_verdict(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery resume verdict")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "resume",
            "--message",
            "fixed the runner; retry grooming",
            "--role",
            "recovery",
            "--stage",
            "recovering",
            "--target-stage",
            "grooming",
            "--task-id",
            task.id,
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    _assert_thread_comments(load_task_thread(tmp_path, updated), [
        TaskThreadComment(
            role="recovery",
            stage="recovering",
            target_stage="grooming",
            verdict="resume",
            message="fixed the runner; retry grooming",
            files_changed=[],
        )
    ])


def test_agent_report_accepts_hidden_step_alias(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent report step alias")

    result = CliRunner().invoke(
        agent_app,
        [
            "report",
            "--verdict",
            "pass",
            "--message",
            "integration report from codex",
            "--role",
            "swe",
            "--step",
            "implementing",
            "--task-id",
            task.id,
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    comments = load_task_thread(tmp_path, updated)
    assert len(comments) == 1
    assert comments[0].role == "swe"
    assert comments[0].stage == "implementing"
    assert comments[0].verdict == "pass"
    assert comments[0].message == "integration report from codex"
    assert comments[0].files_changed == []


def test_root_report_accepts_hidden_step_alias(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Legacy step alias for root report")
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)

    result = CliRunner().invoke(
        root_app,
        [
            "report",
            "--workspace",
            str(tmp_path),
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

    assert result.exit_code == 0, result.output
    updated = get_task_record(tmp_path, task.id)
    assert updated is not None
    _assert_thread_comments(load_task_thread(tmp_path, updated), [
        TaskThreadComment(
            role="recovery",
            stage="recovering",
            verdict="pass",
            message="recovery note",
            files_changed=[],
        )
    ])
