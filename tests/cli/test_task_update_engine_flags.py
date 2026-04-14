import argparse
from pathlib import Path

import pytest

from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task, get_task

from tests.support.helpers import _cmd_update


def test_update_command_rejects_removed_claude_engine_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    ensure_workspace(tmp_path, LitehiveConfig())
    task = create_task(tmp_path, title="Tune Claude task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="claude",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert "update failed: no changes requested" in output


def test_update_command_rejects_removed_goz_engine_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune Goz task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="goz",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert "update failed: no changes requested" in output
