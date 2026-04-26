from pathlib import Path

from litehive.cli.task_logs_support import load_task_with_runtime
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task


def test_load_task_with_runtime_tolerates_unrelated_missing_runtime_rows(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Journal task", auto_commit=False)

    missing_dir = tmp_path / ".litehive" / "tasks" / "T-0002-missing-runtime"
    missing_dir.mkdir(parents=True)

    loaded = load_task_with_runtime(tmp_path, task.id)

    assert loaded is not None
    assert loaded.id == task.id
