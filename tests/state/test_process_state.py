import os
from pathlib import Path

from litehive.config.workspace import create_workspace
from litehive.daemon.registry import register_daemon_for_workspace, unregister_daemon_for_workspace
from litehive.state.locking import touch_runner_status_for_workspace, workspace_runner_guard
from litehive.state.store import runtime_store_for_workspace
from litehive.workspace import Workspace


def test_runner_process_state_is_persisted_in_sqlite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    workspace = tmp_path / "workspace"
    create_workspace(workspace)
    workspace_obj = Workspace.from_path(workspace)
    store = runtime_store_for_workspace(workspace_obj)

    with workspace_runner_guard(workspace_obj):
        payload = store.load_process_state("runner")
        assert payload is not None
        assert payload["pid"] == os.getpid()
        assert payload["status"] == "running"

        touch_runner_status_for_workspace(workspace_obj, active_task_id="T-0001")
        refreshed = store.load_process_state("runner")
        assert refreshed is not None
        assert refreshed["active_task_id"] == "T-0001"

    assert store.load_process_state("runner") is None


def test_daemon_process_state_is_persisted_in_sqlite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    workspace = tmp_path / "workspace"
    create_workspace(workspace)
    workspace_obj = Workspace.from_path(workspace)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    store = runtime_store_for_workspace(workspace_obj)

    try:
        register_daemon_for_workspace(workspace_obj, pid=os.getpid(), log_dir=log_dir)
        payload = store.load_process_state("daemon")
        assert payload is not None
        assert payload["pid"] == os.getpid()
        assert payload["status"] == "running"
        assert payload["workspace"] == str(workspace.resolve())
        assert payload["log_dir"] == str(log_dir)
    finally:
        unregister_daemon_for_workspace(workspace_obj, pid=os.getpid())

    assert store.load_process_state("daemon") is None
