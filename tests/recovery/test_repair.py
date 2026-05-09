from pathlib import Path
import time

from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.workspace import create_workspace
from litehive.recovery.execution_recovery import RunnerRecoveryService
from litehive.state.records import WorkspaceTasks, WorkspaceTasks
from litehive.workspace import Workspace


def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
    del args, kwargs
    raise AssertionError("clean repair should not scan task records")


def test_recover_stale_runner_state_skips_task_scan_for_clean_queue(tmp_path: Path, monkeypatch) -> None:
    create_workspace(tmp_path)
    monkeypatch.setattr(WorkspaceTasks, "list", _boom)
    assert RunnerRecoveryService(Workspace.from_path(tmp_path)).recover_stale_runner_state() is False


def test_repair_clean_workspace_with_100_tasks_skips_task_scan(tmp_path: Path, monkeypatch) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    for index in range(100):
        WorkspaceTasks(workspace).create( title=f"Task {index}")
    monkeypatch.setattr(WorkspaceTasks, "list", _boom)
    start = time.perf_counter()
    result = CliRunner().invoke(app, ["repair", "--workspace", str(tmp_path)], standalone_mode=False)
    elapsed = time.perf_counter() - start
    assert result.return_value == 0
    assert elapsed < 1.0
    assert "repaired: no" in result.output
    assert "active_task_id: None" in result.output
    assert "queue_length: 100" in result.output
    assert "repair completed clean run" in result.output
    assert "no inconsistencies found" in result.output
