"""Tests for litehive list and litehive show commands."""

import argparse
from pathlib import Path

import pytest

from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task, get_task, save_task
from litehive.tasks.archive import archive_task

from tests.support.helpers import _cmd_list, _cmd_recover, _cmd_search, _cmd_show, _cmd_update


def test_list_excludes_done_tasks_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task", auto_commit=False)
    done = create_task(tmp_path, title="Done task", auto_commit=False)
    done.status = "done"
    done.pipeline_status = "done"
    save_task(tmp_path, done)

    exit_code = _cmd_list(
        argparse.Namespace(
            workspace=tmp_path,
            show_all=False,
            filter_status=None,
            filter_pipeline_status=None,
            filter_engine=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert active.id in output
    assert "Active task" in output
    assert done.id not in output


def test_list_all_includes_done_tasks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task", auto_commit=False)
    done = create_task(tmp_path, title="Done task", auto_commit=False)
    done.status = "done"
    done.pipeline_status = "done"
    save_task(tmp_path, done)

    exit_code = _cmd_list(
        argparse.Namespace(
            workspace=tmp_path,
            show_all=True,
            filter_status=None,
            filter_pipeline_status=None,
            filter_engine=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert active.id in output
    assert done.id in output
    assert "Done task" in output


def test_list_filter_by_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Queued task", auto_commit=False)
    interrupted = create_task(tmp_path, title="Interrupted task", auto_commit=False)
    interrupted.status = "interrupted"
    save_task(tmp_path, interrupted)

    exit_code = _cmd_list(
        argparse.Namespace(
            workspace=tmp_path,
            show_all=False,
            filter_status="interrupted",
            filter_pipeline_status=None,
            filter_engine=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert interrupted.id in output
    assert "Interrupted task" in output
    assert queued.id not in output


def test_list_filter_by_archived_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    live = create_task(tmp_path, title="Live task", auto_commit=False)
    archived = create_task(tmp_path, title="Archived task", auto_commit=False)
    archived.status = "done"
    archived.pipeline_status = "done"
    save_task(tmp_path, archived)
    archive_task(tmp_path, archived.id)

    exit_code = _cmd_list(
        argparse.Namespace(
            workspace=tmp_path,
            show_all=False,
            filter_status="archived",
            filter_pipeline_status=None,
            filter_engine=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"{archived.id} [archived/done] Archived task" in output
    assert live.id not in output


def test_list_filter_by_pipeline_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    backlog = create_task(tmp_path, title="Backlog task", auto_commit=False)
    implementing = create_task(tmp_path, title="Implementing task", auto_commit=False)
    implementing.pipeline_status = "implementing"
    save_task(tmp_path, implementing)

    exit_code = _cmd_list(
        argparse.Namespace(
            workspace=tmp_path,
            show_all=False,
            filter_status=None,
            filter_pipeline_status="implementing",
            filter_engine=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert implementing.id in output
    assert "Implementing task" in output
    assert backlog.id not in output


def test_list_filter_by_engine(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="gemini"))
    default_engine = create_task(tmp_path, title="Default engine task", auto_commit=False)
    gemini = create_task(tmp_path, title="Gemini task", auto_commit=False)

    exit_code = _cmd_list(
        argparse.Namespace(
            workspace=tmp_path,
            show_all=False,
            filter_status=None,
            filter_pipeline_status=None,
            filter_engine="gemini",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert default_engine.id in output
    assert "Default engine task" in output
    assert gemini.id in output
    assert "Gemini task" in output


def test_list_composed_filters_use_workspace_default_engine(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    match = create_task(tmp_path, title="Match task", auto_commit=False)
    same_engine = create_task(tmp_path, title="Same engine", auto_commit=False)
    no_match_status = create_task(tmp_path, title="Wrong status", auto_commit=False)
    no_match_status.status = "interrupted"
    save_task(tmp_path, no_match_status)

    exit_code = _cmd_list(
        argparse.Namespace(
            workspace=tmp_path,
            show_all=False,
            filter_status="queued",
            filter_pipeline_status=None,
            filter_engine="codex",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert match.id in output
    assert "Match task" in output
    assert same_engine.id in output
    assert "Same engine" in output
    assert no_match_status.id not in output


def test_list_compact_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Each task prints as one line: ID [status/pipeline_status] title."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="My test task", auto_commit=False)

    exit_code = _cmd_list(
        argparse.Namespace(
            workspace=tmp_path,
            show_all=False,
            filter_status=None,
            filter_pipeline_status=None,
            filter_engine=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"{task.id} [queued/backlog] My test task" in output


def test_show_prints_task_details(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)
    monkeypatch.delenv("LITEHIVE_STAGE", raising=False)
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="gemini"))
    task = create_task(tmp_path, title="Detail task", auto_commit=False)
    task.goal = "Test the show command"
    task.acceptance_criteria = ["criterion one", "criterion two"]
    task.constraints = ["keep it simple"]
    task.plan = ["step one", "step two"]
    task.priority = "high"
    save_task(tmp_path, task)

    exit_code = _cmd_show(argparse.Namespace(workspace=tmp_path, task_id=task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"id: {task.id}" in output
    assert "title: Detail task" in output
    assert "status: queued" in output
    assert "pipeline_stage: backlog" in output
    assert "priority: high" in output
    assert "engine:" not in output
    assert "created_from:" in output
    assert "  source: manual" in output
    assert "  task_id: -" in output
    assert "  role: -" in output
    assert "  rationale: Created outside a Litehive agent session." in output
    assert "goal: Test the show command" in output
    assert "  - criterion one" in output
    assert "  - criterion two" in output
    assert "  - keep it simple" in output
    assert "  - step one" in output
    assert "  - step two" in output


def test_show_prints_agent_creation_provenance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)
    monkeypatch.delenv("LITEHIVE_STAGE", raising=False)
    ensure_workspace(tmp_path)
    parent = create_task(tmp_path, title="Current task", auto_commit=False)
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "planner")
    monkeypatch.setenv("LITEHIVE_TASK_ID", parent.id)
    monkeypatch.setenv("LITEHIVE_STAGE", "grooming")
    created = create_task(tmp_path, title="Spawned from planner", auto_commit=False)

    exit_code = _cmd_show(argparse.Namespace(workspace=tmp_path, task_id=created.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "created_from:" in output
    assert "  source: agent" in output
    assert f"  task_id: {parent.id}" in output
    assert "  stage: grooming" in output
    assert "  role: planner" in output
    assert "  blocking: no" in output
    assert f"  rationale: Created by Litehive agent role planner while working on {parent.id}." in output


def test_show_prints_archived_history(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Archived history task", auto_commit=False)
    task.status = "done"
    task.pipeline_status = "done"
    task.goal = "Keep archived history directly inspectable"
    save_task(tmp_path, task)
    archive_task(tmp_path, task.id)

    exit_code = _cmd_show(argparse.Namespace(workspace=tmp_path, task_id=task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"id: {task.id}" in output
    assert "title: Archived history task" in output
    assert "status: archived" in output
    assert "pipeline_stage: done" in output
    assert "goal: Keep archived history directly inspectable" in output


def test_search_exact_archived_task_id_returns_archived_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Archived search task", auto_commit=False)
    task.status = "done"
    task.pipeline_status = "done"
    task.goal = "Find archived task history by exact task ID"
    save_task(tmp_path, task)
    archive_task(tmp_path, task.id)

    exit_code = _cmd_search(argparse.Namespace(workspace=tmp_path, query=task.id, limit=5))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"{task.id} [archived] Archived search task" in output
    assert "Find archived task history by exact task ID" in output


def test_recover_archived_task_directs_operator_to_create_new_task(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Archived recover task", auto_commit=False)
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)
    archive_task(tmp_path, task.id)

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id=task.id))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert f"recover failed: Task {task.id} is archived and cannot be recovered." in output
    assert "Create a new task for follow-up work instead." in output


def test_task_update_renames_title_in_place(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Original title", auto_commit=False)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            title="Renamed title",
            priority=None,
            goal=None,
            depends_on=None,
            acceptance_criteria=None,
            constraint=None,
            plan_step=None,
            from_file=None,
            edit=False,
        )
    )
    update_output = capsys.readouterr().out

    assert exit_code == 0
    assert f"task: {task.id} Renamed title" in update_output

    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.id == task.id
    assert updated.title == "Renamed title"

    exit_code = _cmd_show(argparse.Namespace(workspace=tmp_path, task_id=task.id))
    show_output = capsys.readouterr().out

    assert exit_code == 0
    assert f"id: {task.id}" in show_output
    assert "title: Renamed title" in show_output


def test_show_displays_dependency_statuses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    live = create_task(tmp_path, title="Live dependency", auto_commit=False)
    live.status = "flagged"
    save_task(tmp_path, live)

    archived = create_task(tmp_path, title="Archived dependency", auto_commit=False)
    archived.status = "done"
    archived.pipeline_status = "done"
    save_task(tmp_path, archived)
    archive_task(tmp_path, archived.id)

    task = create_task(tmp_path, title="Depends on multiple states", auto_commit=False)
    task.depends_on = [live.id, archived.id, "T-9999"]
    save_task(tmp_path, task)

    exit_code = _cmd_show(argparse.Namespace(workspace=tmp_path, task_id=task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"depends_on: {live.id} (flagged), {archived.id} (archived), T-9999 (missing)" in output


def test_show_nonexistent_task_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_show(argparse.Namespace(workspace=tmp_path, task_id="T-9999"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "task not found: T-9999" in output
