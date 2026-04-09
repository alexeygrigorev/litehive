"""Tests for the litehive debug CLI command."""

import argparse
import gzip

import yaml

from tests.workspace_helpers import (
    _cmd_debug,
    _init_git_repo,
    create_task,
    ensure_workspace,
    save_task,
    task_dir,
)
from litehive.models import SubagentRef, TaskThreadComment
from litehive.tasks import append_thread_comment


def _setup_workspace(tmp_path):
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Test task", goal="Test goal")
    return task


def _write_subagent_artifacts(tmp_path, task, sa_id="SA-0001", role="swe", engine="codex",
                               exit_code=0, status="completed", stdout="output here",
                               stderr="", transcript="transcript content",
                               compress=False):
    """Write subagent artifacts and register the ref in task.yaml."""
    sa_path = f"subagents/{sa_id}-{role}"
    base = task_dir(tmp_path, task) / sa_path
    base.mkdir(parents=True, exist_ok=True)

    session_data = {
        "id": sa_id,
        "role": role,
        "engine": engine,
        "status": status,
        "exit_code": exit_code,
        "created_at": "2026-04-09T10:00:00Z",
        "updated_at": "2026-04-09T10:05:00Z",
    }

    if compress:
        with gzip.open(base / "session.yaml.gz", "wt", encoding="utf-8") as f:
            f.write(yaml.safe_dump(session_data, sort_keys=False))
        with gzip.open(base / "stdout.txt.gz", "wt", encoding="utf-8") as f:
            f.write(stdout)
        with gzip.open(base / "stderr.txt.gz", "wt", encoding="utf-8") as f:
            f.write(stderr)
        with gzip.open(base / "transcript.md.gz", "wt", encoding="utf-8") as f:
            f.write(transcript)
    else:
        (base / "session.yaml").write_text(
            yaml.safe_dump(session_data, sort_keys=False), encoding="utf-8"
        )
        (base / "stdout.txt").write_text(stdout, encoding="utf-8")
        (base / "stderr.txt").write_text(stderr, encoding="utf-8")
        (base / "transcript.md").write_text(transcript, encoding="utf-8")

    ref = SubagentRef(id=sa_id, role=role, engine=engine, status=status, path=sa_path)
    task.subagents.append(ref)
    save_task(tmp_path, task)
    return ref


def _debug_args(tmp_path, task_id, show_all=False):
    return argparse.Namespace(
        workspace=tmp_path,
        task_id=task_id,
        show_all=show_all,
    )


def test_debug_latest_subagent(tmp_path, capsys):
    task = _setup_workspace(tmp_path)
    _write_subagent_artifacts(
        tmp_path, task,
        stdout="building project...\nall tests passed",
        transcript="Agent started work on the task and ran tests",
    )

    rc = _cmd_debug(_debug_args(tmp_path, task.id))
    assert rc == 0
    out = capsys.readouterr().out
    assert "engine:    codex" in out
    assert "exit_code: 0" in out
    assert "status:    completed" in out
    assert "Agent started work on the task" in out
    assert "all tests passed" in out


def test_debug_shows_verdict(tmp_path, capsys):
    task = _setup_workspace(tmp_path)
    _write_subagent_artifacts(tmp_path, task)
    append_thread_comment(
        tmp_path, task,
        TaskThreadComment(role="swe", step="implementing", verdict="pass", message="done"),
    )

    rc = _cmd_debug(_debug_args(tmp_path, task.id))
    assert rc == 0
    out = capsys.readouterr().out
    assert "submitted: yes" in out
    assert "verdict:   pass" in out


def test_debug_no_verdict(tmp_path, capsys):
    task = _setup_workspace(tmp_path)
    _write_subagent_artifacts(tmp_path, task)

    rc = _cmd_debug(_debug_args(tmp_path, task.id))
    assert rc == 0
    out = capsys.readouterr().out
    assert "submitted: no" in out


def test_debug_stderr_output(tmp_path, capsys):
    task = _setup_workspace(tmp_path)
    _write_subagent_artifacts(
        tmp_path, task,
        stderr="warning: something went wrong\nerror: critical failure",
    )

    rc = _cmd_debug(_debug_args(tmp_path, task.id))
    assert rc == 0
    out = capsys.readouterr().out
    assert "critical failure" in out


def test_debug_stdout_tail_500(tmp_path, capsys):
    task = _setup_workspace(tmp_path)
    long_stdout = "x" * 1000
    _write_subagent_artifacts(tmp_path, task, stdout=long_stdout)

    rc = _cmd_debug(_debug_args(tmp_path, task.id))
    assert rc == 0
    out = capsys.readouterr().out
    # Should show "..." indicating truncation
    assert "..." in out


def test_debug_transcript_summary_200(tmp_path, capsys):
    task = _setup_workspace(tmp_path)
    long_transcript = "a" * 400
    _write_subagent_artifacts(tmp_path, task, transcript=long_transcript)

    rc = _cmd_debug(_debug_args(tmp_path, task.id))
    assert rc == 0
    out = capsys.readouterr().out
    assert "400 chars total" in out


def test_debug_all_subagents(tmp_path, capsys):
    task = _setup_workspace(tmp_path)
    _write_subagent_artifacts(tmp_path, task, sa_id="SA-0001", role="swe", engine="codex", exit_code=0)
    _write_subagent_artifacts(tmp_path, task, sa_id="SA-0002", role="qa", engine="gemini", exit_code=1, status="failed")

    rc = _cmd_debug(_debug_args(tmp_path, task.id, show_all=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "subagents: 2" in out
    assert "SA-0001" in out
    assert "SA-0002" in out
    assert "role=swe" in out
    assert "role=qa" in out
    assert "engine=codex" in out
    assert "engine=gemini" in out
    assert "status=failed" in out


def test_debug_gzipped_artifacts(tmp_path, capsys):
    task = _setup_workspace(tmp_path)
    _write_subagent_artifacts(
        tmp_path, task,
        stdout="compressed output",
        transcript="compressed transcript",
        compress=True,
    )

    rc = _cmd_debug(_debug_args(tmp_path, task.id))
    assert rc == 0
    out = capsys.readouterr().out
    assert "compressed output" in out
    assert "compressed transcript" in out


def test_debug_no_subagents(tmp_path, capsys):
    task = _setup_workspace(tmp_path)

    rc = _cmd_debug(_debug_args(tmp_path, task.id))
    assert rc == 1
    out = capsys.readouterr().out
    assert "no subagent artifacts" in out


def test_debug_all_no_subagents(tmp_path, capsys):
    task = _setup_workspace(tmp_path)

    rc = _cmd_debug(_debug_args(tmp_path, task.id, show_all=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no subagents" in out


def test_debug_task_not_found(tmp_path, capsys):
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)

    rc = _cmd_debug(_debug_args(tmp_path, "T-9999"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "not found" in out


def test_debug_missing_artifacts_graceful(tmp_path, capsys):
    """Subagent dir exists but individual artifacts are missing."""
    task = _setup_workspace(tmp_path)
    sa_path = "subagents/SA-0001-swe"
    base = task_dir(tmp_path, task) / sa_path
    base.mkdir(parents=True, exist_ok=True)
    # Only write session.yaml, skip everything else
    (base / "session.yaml").write_text(
        yaml.safe_dump({"id": "SA-0001", "role": "swe", "engine": "codex", "status": "completed", "exit_code": 0}, sort_keys=False),
        encoding="utf-8",
    )
    ref = SubagentRef(id="SA-0001", role="swe", engine="codex", status="completed", path=sa_path)
    task.subagents.append(ref)
    save_task(tmp_path, task)

    rc = _cmd_debug(_debug_args(tmp_path, task.id))
    assert rc == 0
    out = capsys.readouterr().out
    assert "engine:    codex" in out
    assert "(not found)" in out
