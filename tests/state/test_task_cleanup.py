from pathlib import Path
import shutil

from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task, discard_created_task
from litehive.tasks.paths import task_dir


def test_discard_created_task_missing_dir_no_error(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Ephemeral")
    td = task_dir(tmp_path, task)
    shutil.rmtree(td)
    assert not td.exists()

    discard_created_task(tmp_path, task.id)


def test_discard_created_task_existing_dir_removed(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Removable")
    td = task_dir(tmp_path, task)
    assert td.exists()

    discard_created_task(tmp_path, task.id)

    assert not td.exists()
