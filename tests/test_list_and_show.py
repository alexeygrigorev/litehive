"""Tests for litehive list and litehive show commands."""

from tests.workspace_helpers import (
    Path,
    _cmd_list,
    _cmd_show,
    argparse,
    archive_task,
    create_task,
    ensure_workspace,
    pytest,
    save_task,
)


def test_list_excludes_done_tasks_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task", auto_commit=False)
    done = create_task(tmp_path, title="Done task", auto_commit=False)
    done.status = "done"
    done.pipeline_status = "done"
    save_task(tmp_path, done)

    exit_code = _cmd_list(argparse.Namespace(
        workspace=tmp_path, show_all=False,
        filter_status=None, filter_pipeline_status=None, filter_engine=None,
    ))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert active.id in output
    assert "Active task" in output
    assert done.id not in output


def test_list_all_includes_done_tasks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task", auto_commit=False)
    done = create_task(tmp_path, title="Done task", auto_commit=False)
    done.status = "done"
    done.pipeline_status = "done"
    save_task(tmp_path, done)

    exit_code = _cmd_list(argparse.Namespace(
        workspace=tmp_path, show_all=True,
        filter_status=None, filter_pipeline_status=None, filter_engine=None,
    ))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert active.id in output
    assert done.id in output
    assert "Done task" in output


def test_list_filter_by_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Queued task", auto_commit=False)
    interrupted = create_task(tmp_path, title="Interrupted task", auto_commit=False)
    interrupted.status = "interrupted"
    save_task(tmp_path, interrupted)

    exit_code = _cmd_list(argparse.Namespace(
        workspace=tmp_path, show_all=False,
        filter_status="interrupted", filter_pipeline_status=None, filter_engine=None,
    ))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert interrupted.id in output
    assert "Interrupted task" in output
    assert queued.id not in output


def test_list_filter_by_pipeline_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    backlog = create_task(tmp_path, title="Backlog task", auto_commit=False)
    implementing = create_task(tmp_path, title="Implementing task", auto_commit=False)
    implementing.pipeline_status = "implementing"
    save_task(tmp_path, implementing)

    exit_code = _cmd_list(argparse.Namespace(
        workspace=tmp_path, show_all=False,
        filter_status=None, filter_pipeline_status="implementing", filter_engine=None,
    ))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert implementing.id in output
    assert "Implementing task" in output
    assert backlog.id not in output


def test_list_filter_by_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    default_engine = create_task(tmp_path, title="Default engine task", auto_commit=False)
    gemini = create_task(tmp_path, title="Gemini task", engine="gemini", auto_commit=False)

    exit_code = _cmd_list(argparse.Namespace(
        workspace=tmp_path, show_all=False,
        filter_status=None, filter_pipeline_status=None, filter_engine="gemini",
    ))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert gemini.id in output
    assert "Gemini task" in output
    assert default_engine.id not in output


def test_list_composed_filters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    match = create_task(tmp_path, title="Match task", engine="codex", auto_commit=False)
    no_match_engine = create_task(tmp_path, title="Wrong engine", engine="gemini", auto_commit=False)
    no_match_status = create_task(tmp_path, title="Wrong status", engine="codex", auto_commit=False)
    no_match_status.status = "interrupted"
    save_task(tmp_path, no_match_status)

    exit_code = _cmd_list(argparse.Namespace(
        workspace=tmp_path, show_all=False,
        filter_status="queued", filter_pipeline_status=None, filter_engine="codex",
    ))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert match.id in output
    assert "Match task" in output
    assert no_match_engine.id not in output
    assert no_match_status.id not in output


def test_list_compact_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each task prints as one line: ID [status/pipeline_status] title."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="My test task", auto_commit=False)

    exit_code = _cmd_list(argparse.Namespace(
        workspace=tmp_path, show_all=False,
        filter_status=None, filter_pipeline_status=None, filter_engine=None,
    ))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"{task.id} [queued/backlog] My test task" in output


def test_show_prints_task_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Detail task", engine="gemini", auto_commit=False)
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
    assert "pipeline_status: backlog" in output
    assert "priority: high" in output
    assert "engine: gemini" in output
    assert "goal: Test the show command" in output
    assert "  - criterion one" in output
    assert "  - criterion two" in output
    assert "  - keep it simple" in output
    assert "  - step one" in output
    assert "  - step two" in output


def test_show_displays_dependency_statuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
    assert (
        f"depends_on: {live.id} (flagged), {archived.id} (archived), T-9999 (missing)"
        in output
    )


def test_show_nonexistent_task_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_show(argparse.Namespace(workspace=tmp_path, task_id="T-9999"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "task not found: T-9999" in output
