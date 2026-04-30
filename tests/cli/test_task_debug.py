"""Tests for litehive debug command."""

import argparse
import gzip
from pathlib import Path

import pytest

from litehive.agents.session_store import save_subagent_artifacts
from heru.types import SubagentRef

from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import StageReport, TaskActivityEntry
from litehive.state.records import create_task, save_task
from litehive.tasks.paths import task_dir
from litehive.tasks.reports import append_activity_entry, record_stage_report

from tests.support.helpers import _cmd_debug, _run, _task_worktree_path


def _ns(workspace, task_id, all_flag=False, worktree_flag=False):
    return argparse.Namespace(
        workspace=workspace,
        task_id=task_id,
        all=all_flag,
        worktree=worktree_flag,
    )


def _make_task_with_subagent(tmp_path, *, engine="codex", role="swe", sa_id="SA-implementing"):
    """Create a task with one subagent ref and its artifact directory."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Debug test task", auto_commit=False)
    sa_path = f"subagents/{sa_id}"
    task.subagents = [SubagentRef(id=sa_id, role=role, engine=engine, status="completed", path=sa_path)]
    save_task(tmp_path, task)

    # Create subagent artifact directory
    sa_dir = task_dir(tmp_path, task) / sa_path
    sa_dir.mkdir(parents=True, exist_ok=True)
    return task, sa_dir


def _write_session_record(
    root: Path, task_id: str, *, sa_id="SA-implementing", role="swe", engine="codex", status="completed", exit_code=0
):
    """Write a subagent session record into SQLite storage."""
    save_subagent_artifacts(
        root,
        task_id,
        sa_id,
        session={
            "id": sa_id,
            "role": role,
            "engine": engine,
            "status": status,
            "exit_code": exit_code,
            "created_at": "2026-04-09T10:00:00Z",
            "updated_at": "2026-04-09T10:05:00Z",
        },
    )


def _init_git_repo(root: Path) -> None:
    _run(["git", "init"], root)
    _run(["git", "config", "user.name", "Test User"], root)
    _run(["git", "config", "user.email", "test@example.com"], root)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _run(["git", "add", "README.md"], root)
    _run(["git", "commit", "-m", "initial"], root)


def _create_task_worktree(root: Path, task) -> Path:
    worktree_path = _task_worktree_path(root, task)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], root)
    task.git.worktree_path = str(worktree_path)
    save_task(root, task)
    return worktree_path


# -- Basic latest subagent display --


def test_debug_latest_subagent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, sa_dir = _make_task_with_subagent(tmp_path)
    (sa_dir / "stdout.txt").write_text("hello world", encoding="utf-8")

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"task: {task.id}" in output
    assert "subagent: SA-implementing" in output
    assert "engine: codex" in output
    assert "role: swe" in output
    assert "status: completed" in output


def test_debug_task_not_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    exit_code = _cmd_debug(_ns(tmp_path, "T-9999"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "task not found: T-9999" in output


def test_debug_no_subagents(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Empty task", auto_commit=False)

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "no subagents" in output


# -- Verdict display --


def test_debug_shows_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, sa_dir = _make_task_with_subagent(tmp_path)
    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(role="swe", stage="implementing", verdict="pass", message="All good"),
    )

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "verdict: pass" in output
    assert "verdict_stage: implementing" in output
    assert "verdict_message: All good" in output


def test_debug_reads_verdict_through_activity_boundary(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task, sa_dir = _make_task_with_subagent(tmp_path)

    monkeypatch.setattr(
        "litehive.cli.task_debug_support.latest_task_activity_entry",
        lambda *args, **kwargs: TaskActivityEntry(
            role="swe",
            stage="implementing",
            verdict="pass",
            message="Boundary verdict",
        ),
    )

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "verdict: pass" in output
    assert "verdict_stage: implementing" in output
    assert "verdict_message: Boundary verdict" in output


def test_debug_no_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, sa_dir = _make_task_with_subagent(tmp_path)

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "verdict: none" in output


def test_debug_shows_latest_hook_stage_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, sa_dir = _make_task_with_subagent(tmp_path)
    record_stage_report(
        tmp_path,
        task,
        StageReport(
            task_id=task.id,
            pipeline_state="accepting",
            verdict="reject",
            source="hook",
            summary="accepting rejected: hook `uv run ruff check .` failed at `before_accepting`",
            feedback="Runner hook failure at `before_accepting`",
        ),
    )

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "stage_report_verdict: reject" in output
    assert "stage_report_source: hook" in output
    assert "stage_report_pipeline_state: accepting" in output
    assert "stage_report_summary: accepting rejected: hook `uv run ruff check .` failed at `before_accepting`" in output


# -- stdout/stderr tail --


def test_debug_stdout_tail_500(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, sa_dir = _make_task_with_subagent(tmp_path)
    long_output = "x" * 1000
    (sa_dir / "stdout.txt").write_text(long_output, encoding="utf-8")

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "stdout (last 500 of 1000 chars):" in output
    assert "..." in output  # truncation indicator


def test_debug_stderr_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, sa_dir = _make_task_with_subagent(tmp_path)
    (sa_dir / "stderr.txt").write_text("error: something failed", encoding="utf-8")

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "error: something failed" in output


# -- Execution trace summary --


def test_debug_execution_trace_summary_200(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, sa_dir = _make_task_with_subagent(tmp_path)
    long_execution_trace = "A" * 500
    (sa_dir / "stdout.txt").write_text(long_execution_trace, encoding="utf-8")

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "execution trace (500 chars, showing first 200):" in output
    assert "A" * 200 in output


# -- Gzipped artifacts --


def test_debug_gzipped_artifacts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, sa_dir = _make_task_with_subagent(tmp_path)

    # Write gzipped stdout
    with gzip.open(sa_dir / "stdout.txt.gz", "wt", encoding="utf-8") as f:
        f.write("gzipped stdout content")

    # Write gzipped execution trace
    with gzip.open(sa_dir / "execution_trace.md.gz", "wt", encoding="utf-8") as f:
        f.write("gzipped transcript content")

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "gzipped stdout content" in output
    assert "gzipped transcript content" in output


# -- Missing artifacts graceful handling --


def test_debug_missing_artifacts_graceful(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, sa_dir = _make_task_with_subagent(tmp_path)
    # No artifacts written — should not crash

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "stdout: (not found)" in output
    assert "stderr: (not found)" in output
    assert "execution trace: (not found)" in output


# -- Session record fallback for exit_code --


def test_debug_session_yaml_fallback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When no runtime state exists, fall back to stored session data for exit_code and timing."""
    task, sa_dir = _make_task_with_subagent(tmp_path)
    _write_session_record(tmp_path, task.id, exit_code=0)

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "exit_code: 0" in output
    assert "created_at: 2026-04-09T10:00:00Z" in output


# -- --all flag --


def test_debug_all_subagents(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Multi SA task", auto_commit=False)
    sa_path_1 = "subagents/SA-grooming"
    sa_path_2 = "subagents/SA-implementing"
    sa_path_3 = "subagents/SA-testing"
    task.subagents = [
        SubagentRef(
            id="SA-grooming",
            role="planner",
            engine="gemini",
            status="completed",
            path=sa_path_1,
        ),
        SubagentRef(
            id="SA-implementing",
            role="swe",
            engine="codex",
            status="completed",
            path=sa_path_2,
        ),
        SubagentRef(
            id="SA-testing",
            role="qa",
            engine="claude",
            status="failed",
            path=sa_path_3,
        ),
    ]
    save_task(tmp_path, task)

    # Write subagent session records with exit_code for each to test display in --all
    td = task_dir(tmp_path, task)
    for sa_path, sa_id, role, engine, ec in [
        (sa_path_1, "SA-grooming", "planner", "gemini", 0),
        (sa_path_2, "SA-implementing", "swe", "codex", 0),
        (sa_path_3, "SA-testing", "qa", "claude", 1),
    ]:
        d = td / sa_path
        d.mkdir(parents=True, exist_ok=True)
        _write_session_record(tmp_path, task.id, sa_id=sa_id, role=role, engine=engine, exit_code=ec)

    exit_code = _cmd_debug(_ns(tmp_path, task.id, all_flag=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "3 subagent(s)" in output
    assert "SA-grooming" in output
    assert "SA-implementing" in output
    assert "SA-testing" in output
    assert "role=planner" in output
    assert "engine=codex" in output
    assert "status=failed" in output
    assert "exit_code=0" in output
    assert "exit_code=1" in output


def test_debug_all_no_subagents(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="No SA task", auto_commit=False)

    exit_code = _cmd_debug(_ns(tmp_path, task.id, all_flag=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "no subagents" in output


# -- Runtime subagent state (exit code, timing) --


def test_debug_shows_exit_code_from_session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, sa_dir = _make_task_with_subagent(tmp_path)
    _write_session_record(tmp_path, task.id, exit_code=0)

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "exit_code: 0" in output
    assert "created_at: 2026-04-09T10:00:00Z" in output
    assert "session_updated_at: 2026-04-09T10:05:00Z" in output


def test_debug_worktree_shows_uncommitted_changes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    task = create_task(tmp_path, title="Worktree debug task", auto_commit=False)
    worktree_path = _create_task_worktree(tmp_path, task)
    (worktree_path / "dirty.py").write_text("print('dirty')\n", encoding="utf-8")

    exit_code = _cmd_debug(_ns(tmp_path, task.id, worktree_flag=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"task: {task.id}" in output
    assert f"worktree: {task.runtime.pipeline.git.worktree_path}" in output
    assert "exists: yes" in output
    assert "uncommitted:" in output
    assert "  - dirty.py" in output
    assert "committed_ahead_of_main:" in output
    assert "  (none)" in output


def test_debug_worktree_shows_committed_changes_ahead_of_main(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    task = create_task(tmp_path, title="Committed worktree debug task", auto_commit=False)
    worktree_path = _create_task_worktree(tmp_path, task)
    (worktree_path / "feature.py").write_text("print('feature')\n", encoding="utf-8")
    _run(["git", "add", "feature.py"], worktree_path)
    _run(["git", "commit", "-m", "feature commit"], worktree_path)

    exit_code = _cmd_debug(_ns(tmp_path, task.id, worktree_flag=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "exists: yes" in output
    assert "uncommitted:" in output
    assert "committed_ahead_of_main:" in output
    assert "  - feature.py" in output


def test_debug_worktree_shows_no_worktree_when_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Missing worktree task", auto_commit=False)
    task.git.worktree_path = ".litehive/worktrees/missing-task"
    save_task(tmp_path, task)

    exit_code = _cmd_debug(_ns(tmp_path, task.id, worktree_flag=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "no worktree" in output
