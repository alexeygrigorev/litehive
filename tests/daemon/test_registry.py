import os
from pathlib import Path

import yaml

from litehive.config.paths import litehive_root
from litehive.config.workspace import ensure_workspace
from litehive.daemon.execution import start_background_daemon
from litehive.daemon.registry import (
    daemon_lock_is_active,
    daemon_lock_path,
    daemon_metadata,
    get_workspace_daemon,
    list_daemon_instances,
    register_daemon,
    unregister_daemon,
)


def test_register_and_unregister_daemon_uses_shared_lock_manager(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    register_daemon(workspace, pid=os.getpid(), log_dir=log_dir)

    entry = daemon_metadata(workspace)
    assert entry is not None
    assert entry["status"] == "running"
    assert entry["pid"] == os.getpid()
    assert entry["log_dir"] == str(log_dir)
    assert daemon_lock_is_active(workspace) is True
    assert get_workspace_daemon(workspace) == entry
    assert [item["workspace"] for item in list_daemon_instances()] == [str(workspace.resolve())]

    unregister_daemon(workspace, pid=os.getpid())

    assert daemon_metadata(workspace) is None
    assert daemon_lock_is_active(workspace) is False
    assert get_workspace_daemon(workspace) is None
    assert list_daemon_instances() == []
    assert daemon_lock_path(workspace).read_text(encoding="utf-8") == ""


def test_unregister_daemon_clears_stale_metadata_and_registry_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    lock_path = daemon_lock_path(workspace)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        yaml.safe_dump(
            {
                "workspace": str(workspace.resolve()),
                "pid": 424242,
                "started_at": "2026-04-12T00:00:00Z",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    registry_path = litehive_root() / "daemons.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            [
                {
                    "workspace": str(workspace.resolve()),
                    "pid": 424242,
                    "started_at": "2026-04-12T00:00:00Z",
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("litehive.daemon.registry.pid_is_alive", lambda pid: False)

    unregister_daemon(workspace, pid=424242)

    assert lock_path.read_text(encoding="utf-8") == ""
    assert yaml.safe_load(registry_path.read_text(encoding="utf-8")) == []


def test_start_background_daemon_strips_agent_env(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.setenv("LITEHIVE_STAGE", "implementing")
    monkeypatch.setenv("LITEHIVE_TASK_ID", "T-0446")
    monkeypatch.setattr("litehive.daemon.execution.daemon_metadata", lambda workspace: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda workspace: None)
    monkeypatch.setattr("litehive.daemon.execution._ensure_workspace_venvs_ready", lambda *args, **kwargs: None)

    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 4321

        def poll(self) -> None:
            return None

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("litehive.daemon.execution.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "litehive.daemon.execution.get_workspace_daemon",
        lambda workspace: {"pid": 4321, "status": "running"},
    )

    pid = start_background_daemon(tmp_path)

    assert pid == 4321
    child_env = captured["kwargs"]["env"]
    assert "LITEHIVE_AGENT_ROLE" not in child_env
    assert "LITEHIVE_STAGE" not in child_env
    assert "LITEHIVE_TASK_ID" not in child_env
    assert child_env["LITEHIVE_WORKSPACE_ROOT"] == str(tmp_path.resolve())
