"""Tests for the minimal task evidence view."""

import argparse
from pathlib import Path

import pytest

from heru.types import SubagentRef

from litehive.agents.session_store import save_subagent_artifacts
from litehive.config.workspace import ensure_workspace
from litehive.domain.recovery import FailureFingerprint, RecoveryTrigger, TriggerEventKind
from litehive.domain.reports import StageReport, TaskActivityEntry
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.workspace import Workspace
from litehive.lifecycle.types import PipelineMode
from litehive.state.records import create_task, save_task
from litehive.tasks.activity_rendering import append_activity_entry
from litehive.tasks.paths import task_dir
from litehive.tasks.report_storage import record_stage_report

from tests.support.helpers import _cmd_debug, _cmd_evidence, _run, _task_worktree_path


def _ns(workspace, task_id, all_flag=False, worktree_flag=False):
    return argparse.Namespace(
        workspace=workspace,
        task_id=task_id,
        all=all_flag,
        worktree=worktree_flag,
    )


def _make_task_with_subagent(tmp_path, *, engine="codex", role="swe", sa_id="SA-implementing"):
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Debug test task", auto_commit=False)
    sa_path = f"subagents/{sa_id}"
    task.subagents = [SubagentRef(id=sa_id, role=role, engine=engine, status="completed", path=sa_path)]
    save_task(tmp_path, task)

    sa_dir = task_dir(tmp_path, task) / sa_path
    sa_dir.mkdir(parents=True, exist_ok=True)
    return task, sa_dir


def _write_session_record(
    root: Path, task_id: str, *, sa_id="SA-implementing", role="swe", engine="codex", status="completed", exit_code=0
):
    save_subagent_artifacts(Workspace.from_path(root),
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


def test_task_evidence_renders_minimal_recovery_routing_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task, sa_dir = _make_task_with_subagent(tmp_path)
    (sa_dir / "stdout.txt").write_text("very verbose output that should not be printed\n", encoding="utf-8")
    _write_session_record(tmp_path, task.id, exit_code=17)
    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(role="swe", stage="implementing", verdict="reject", message="agent report failed"),
    )
    record_stage_report(Workspace.from_path(tmp_path),
        task,
        StageReport(
            task_id=task.id,
            pipeline_state="implementing",
            verdict="reject",
            source="agent",
            summary="implementing rejected: report CLI failed",
        ),
    )
    state = SqlitePersistence(Workspace.from_path(tmp_path)).initialize(task.id, pipeline_mode=PipelineMode.FULL)
    state.stage = "recovering"
    state.failed_reason = "recovery_crashed"
    state.failed_message = "recovery crashed while routing"
    state.active_recovery_trigger = RecoveryTrigger(
        origin_stage="implementing",
        trigger_event_kind=TriggerEventKind.CRASH,
        failure_fingerprint=FailureFingerprint(
            fingerprint="RuntimeError:boom",
            classification="RuntimeError",
        ),
        reason_code="stage_exception",
        message="agent crashed before report",
    )
    SqlitePersistence(Workspace.from_path(tmp_path)).save(state)

    exit_code = _cmd_evidence(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"task: {task.id}" in output
    assert "lifecycle_stage: recovering" in output
    assert "failed_reason: recovery_crashed" in output
    assert "active_recovery_trigger: origin_stage=implementing kind=crash" in output
    assert "latest_stage_report: stage=implementing verdict=reject source=agent" in output
    assert "latest_activity: stage=implementing role=swe verdict=reject message=agent report failed" in output
    assert "latest_subagent: id=SA-implementing role=swe engine=codex status=completed exit_code=17" in output
    assert "produced_output=yes" in output
    assert "very verbose output" not in output
    assert "stdout (last" not in output


def test_debug_alias_uses_minimal_evidence_view(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task, _sa_dir = _make_task_with_subagent(tmp_path)

    exit_code = _cmd_debug(_ns(tmp_path, task.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"task: {task.id}" in output
    assert "latest_subagent: id=SA-implementing role=swe engine=codex status=completed" in output
    assert "execution trace:" not in output
    assert "stdout:" not in output


def test_debug_task_not_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    exit_code = _cmd_debug(_ns(tmp_path, "T-9999"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "task not found: T-9999" in output


def test_debug_all_subagents_remains_compact(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Multi SA task", auto_commit=False)
    task.subagents = [
        SubagentRef(
            id="SA-grooming", role="planner", engine="gemini", status="completed", path="subagents/SA-grooming"
        ),
        SubagentRef(id="SA-testing", role="qa", engine="claude", status="failed", path="subagents/SA-testing"),
    ]
    save_task(tmp_path, task)
    _write_session_record(tmp_path, task.id, sa_id="SA-grooming", role="planner", engine="gemini", exit_code=0)
    _write_session_record(tmp_path, task.id, sa_id="SA-testing", role="qa", engine="claude", exit_code=1)

    exit_code = _cmd_debug(_ns(tmp_path, task.id, all_flag=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "2 subagent(s)" in output
    assert "SA-grooming" in output
    assert "SA-testing" in output
    assert "exit_code=1" in output


def test_debug_worktree_shows_minimal_change_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    task = create_task(tmp_path, title="Worktree debug task", auto_commit=False)
    worktree_path = _create_task_worktree(tmp_path, task)
    (worktree_path / "dirty.py").write_text("print('dirty')\n", encoding="utf-8")

    exit_code = _cmd_debug(_ns(tmp_path, task.id, worktree_flag=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"task: {task.id}" in output
    assert "exists=yes uncommitted=1 committed_ahead_of_main=0" in output
    assert "worktree_uncommitted: dirty.py" in output
