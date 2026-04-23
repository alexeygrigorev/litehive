"""Tests for the `litehive task browse` command."""

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task, save_task

from tests.support.helpers import _cmd_browse


def test_task_browse_default_window_lists_recently_created_tasks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)
    monkeypatch.delenv("LITEHIVE_STAGE", raising=False)
    ensure_workspace(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)

    recent = create_task(tmp_path, title="Recent browse task", auto_commit=False)
    recent.created_at = (now - timedelta(hours=2)).isoformat()
    save_task(tmp_path, recent)

    stale = create_task(tmp_path, title="Stale browse task", auto_commit=False)
    stale.created_at = (now - timedelta(days=2)).isoformat()
    save_task(tmp_path, stale)

    exit_code = _cmd_browse(argparse.Namespace(workspace=tmp_path, since=None))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "TASK_ID" in output
    assert "TITLE" in output
    assert "CREATED_AT" in output
    assert "SOURCE" in output
    assert "CONTEXT" in output
    assert recent.id in output
    assert "Recent browse task" in output
    assert recent.created_at in output
    assert "manual" in output
    assert stale.id not in output
    assert "Stale browse task" not in output


def test_task_browse_renders_agent_creation_context(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)
    monkeypatch.delenv("LITEHIVE_STAGE", raising=False)
    ensure_workspace(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)

    parent = create_task(tmp_path, title="Current parent task", auto_commit=False)
    parent.created_at = (now - timedelta(hours=3)).isoformat()
    save_task(tmp_path, parent)

    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "planner")
    monkeypatch.setenv("LITEHIVE_TASK_ID", parent.id)
    monkeypatch.setenv("LITEHIVE_STAGE", "grooming")
    created = create_task(tmp_path, title="Agent-created child", auto_commit=False)
    created.created_at = (now - timedelta(minutes=30)).isoformat()
    save_task(tmp_path, created)

    exit_code = _cmd_browse(argparse.Namespace(workspace=tmp_path, since="24h"))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert created.id in output
    assert "Agent-created child" in output
    assert created.created_at in output
    assert "agent" in output
    assert f"{parent.id} Current parent task" in output
    assert "stage=grooming" in output
    assert "role=planner" in output


def test_task_browse_rejects_invalid_since(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_browse(argparse.Namespace(workspace=tmp_path, since="yesterday"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "browse failed: Invalid duration format 'yesterday'" in output
