from pathlib import Path

from litehive.config.workspace import create_workspace
from litehive.container import build_container
from litehive.state.records import WorkspaceTasks


def test_build_container_injects_workspace_tasks_with_same_workspace(tmp_path: Path) -> None:
    create_workspace(tmp_path)

    container = build_container(tmp_path)

    assert isinstance(container.tasks, WorkspaceTasks)
    assert container.tasks.workspace is container.workspace

