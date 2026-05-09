"""Tests for litehive list and litehive show commands."""

import argparse
from pathlib import Path

import pytest
from typer.testing import CliRunner

from litehive.cli.task_cli import app as task_app
from litehive.config.workspace import create_workspace
from litehive.state.records import WorkspaceTasks
from litehive.workspace import Workspace

from tests.support.helpers import _cmd_list, _cmd_recover, _cmd_show, _cmd_update
from litehive.domain.common import PipelineStatus, TaskStatus


def _help_option_names(output: str) -> set[str]:
    import re

    return set(re.findall(r"(?<![\w-])--[\w-]+", output))


def test_list_help_matches_trimmed_option_surface() -> None:
    result = CliRunner().invoke(task_app, ["list", "--help"], standalone_mode=False)

    assert result.exit_code == 0, result.output
    option_names = _help_option_names(result.output)
    assert "--all" in option_names
    for option in ["--status", "--pipeline-status", "--engine"]:
        assert option not in option_names


def test_list_excludes_done_tasks_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    create_workspace(tmp_path)
    active = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Active task", auto_commit=False)
    done = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Done task", auto_commit=False)
    done.status = TaskStatus.DONE
    done.pipeline_status = PipelineStatus.DONE
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(done)

    exit_code = _cmd_list(
        argparse.Namespace(
            workspace=tmp_path,
            show_all=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert active.id in output
    assert "Active task" in output
    assert done.id not in output


def test_list_all_includes_done_tasks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    create_workspace(tmp_path)
    active = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Active task", auto_commit=False)
    done = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Done task", auto_commit=False)
    done.status = TaskStatus.DONE
    done.pipeline_status = PipelineStatus.DONE
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(done)

    exit_code = _cmd_list(
        argparse.Namespace(
            workspace=tmp_path,
            show_all=True,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert active.id in output
    assert done.id in output
    assert "Done task" in output


def test_list_compact_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Each task prints as one line: ID [status/pipeline_status] title."""
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="My test task", auto_commit=False)

    exit_code = _cmd_list(
        argparse.Namespace(
            workspace=tmp_path,
            show_all=False,
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
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Detail task", auto_commit=False)
    task.goal = "Test the show command"
    task.acceptance_criteria = ["criterion one", "criterion two"]
    task.constraints = ["keep it simple"]
    task.plan = ["step one", "step two"]
    task.priority = "high"
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(task)

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
    create_workspace(tmp_path)
    parent = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Current task", auto_commit=False)
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "planner")
    monkeypatch.setenv("LITEHIVE_TASK_ID", parent.id)
    monkeypatch.setenv("LITEHIVE_STAGE", "grooming")
    created = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Spawned from planner", auto_commit=False)

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


def test_show_prints_done_history(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Done history task", auto_commit=False)
    task.status = TaskStatus.DONE
    task.pipeline_status = PipelineStatus.DONE
    task.goal = "Keep completed history directly inspectable"
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(task)

    exit_code = _cmd_show(argparse.Namespace(workspace=tmp_path, task_id=task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"id: {task.id}" in output
    assert "title: Done history task" in output
    assert "status: done" in output
    assert "close_reason: done" in output
    assert "pipeline_stage: done" in output
    assert "goal: Keep completed history directly inspectable" in output


def test_recover_done_task_requeues_follow_up_work(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Done recover task", auto_commit=False)
    task.status = TaskStatus.DONE
    task.pipeline_status = PipelineStatus.DONE
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(task)

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id=task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"task: {task.id} Done recover task" in output
    assert "status: queued" in output
    assert "pipeline_stage: implementing" in output


def test_task_update_renames_title_in_place(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Original title", auto_commit=False)

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

    updated = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert updated is not None
    assert updated.id == task.id
    assert updated.title == "Renamed title"

    exit_code = _cmd_show(argparse.Namespace(workspace=tmp_path, task_id=task.id))
    show_output = capsys.readouterr().out

    assert exit_code == 0
    assert f"id: {task.id}" in show_output
    assert "title: Renamed title" in show_output


def test_show_displays_dependency_statuses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    create_workspace(tmp_path)
    live = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Live dependency", auto_commit=False)
    live.status = TaskStatus.FLAGGED
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(live)

    done = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Done dependency", auto_commit=False)
    done.status = TaskStatus.DONE
    done.pipeline_status = PipelineStatus.DONE
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(done)

    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Depends on multiple states", auto_commit=False)
    task.depends_on = [live.id, done.id, "T-9999"]
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(task)

    exit_code = _cmd_show(argparse.Namespace(workspace=tmp_path, task_id=task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"depends_on: {live.id} (flagged), {done.id} (done), T-9999 (missing)" in output


def test_show_nonexistent_task_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    create_workspace(tmp_path)

    exit_code = _cmd_show(argparse.Namespace(workspace=tmp_path, task_id="T-9999"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "task not found: T-9999" in output
