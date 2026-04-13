import time
from pathlib import Path

from litehive.cli.status import cmd_repair
from litehive.recovery.workspace_repair import recover_stale_runner_state
from litehive.tasks.crud import create_task
from tests.workspace_helpers import ensure_workspace


def test_recover_stale_runner_state_skips_task_scan_for_clean_queue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ensure_workspace(tmp_path)

    def _boom(root, *, include_runtime=True):  # type: ignore[no-untyped-def]
        raise AssertionError("clean repair should not scan task records")

    monkeypatch.setattr("litehive.tasks.crud.list_tasks", _boom)

    assert recover_stale_runner_state(tmp_path) is False


def test_repair_clean_workspace_with_100_tasks_stays_under_budget(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    for index in range(100):
        create_task(tmp_path, title=f"Task {index}")

    started = time.perf_counter()
    exit_code = cmd_repair(tmp_path)
    elapsed = time.perf_counter() - started

    assert exit_code == 0
    assert elapsed < 1.0
