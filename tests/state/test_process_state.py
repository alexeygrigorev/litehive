import os
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.daemon.registry import register_daemon, unregister_daemon
from litehive.state.locking import touch_runner_status, workspace_runner_guard
from litehive.state.store import runtime_store


def test_runner_process_state_is_persisted_in_sqlite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    store = runtime_store(workspace)

    with workspace_runner_guard(workspace):
        payload = store.load_process_state("runner")
        assert payload is not None
        assert payload["pid"] == os.getpid()
        assert payload["status"] == "running"

        touch_runner_status(workspace, active_task_id="T-0001")
        refreshed = store.load_process_state("runner")
        assert refreshed is not None
        assert refreshed["active_task_id"] == "T-0001"

    assert store.load_process_state("runner") is None


def test_daemon_process_state_is_persisted_in_sqlite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    store = runtime_store(workspace)

    try:
        register_daemon(workspace, pid=os.getpid(), log_dir=log_dir)
        payload = store.load_process_state("daemon")
        assert payload is not None
        assert payload["pid"] == os.getpid()
        assert payload["status"] == "running"
        assert payload["workspace"] == str(workspace.resolve())
        assert payload["log_dir"] == str(log_dir)
    finally:
        unregister_daemon(workspace, pid=os.getpid())

    assert store.load_process_state("daemon") is None
