from pathlib import Path
import time

from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.workspace import ensure_workspace
from litehive.recovery.execution_recovery import recover_stale_runner_state
from litehive.state.records import create_task


def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
    del args, kwargs
    raise AssertionError("clean repair should not scan task records")


def test_recover_stale_runner_state_skips_task_scan_for_clean_queue(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    monkeypatch.setattr("litehive.state.records.list_tasks", _boom)
    assert recover_stale_runner_state(tmp_path) is False


def test_repair_clean_workspace_with_100_tasks_skips_task_scan(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    for index in range(100):
        create_task(tmp_path, title=f"Task {index}")
    monkeypatch.setattr("litehive.state.records.list_tasks", _boom)
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
