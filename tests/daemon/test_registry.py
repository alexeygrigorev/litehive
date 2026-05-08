import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from litehive.config.model import DaemonConfig, LitehiveConfig
from litehive.config.workspace import create_workspace
from litehive.daemon.execution import start_background_daemon, stop_workspace_daemon
from litehive.daemon.registry import (
    DaemonRegistryEntry,
    daemon_lock_is_active_for_workspace,
    daemon_lock_path,
    daemon_metadata_for_workspace,
    get_workspace_daemon_for_workspace,
    register_daemon_for_workspace,
    unregister_daemon_for_workspace,
)
from litehive.workspace import Workspace


def _spawn_locked_daemon_like_process(
    workspace: Path,
    metadata: dict[str, object],
) -> subprocess.Popen[str]:
    lock_path = daemon_lock_path(workspace)
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,json,os,signal,sys,time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "lock_path = Path(sys.argv[1])\n"
            "workspace = Path(sys.argv[2]).resolve()\n"
            "metadata = json.loads(sys.argv[3])\n"
            "lock_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "handle = lock_path.open('a+', encoding='utf-8')\n"
            "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
            "payload = {\n"
            "    'workspace': str(workspace),\n"
            "    'pid': os.getpid(),\n"
            "    'started_at': '2026-04-12T00:00:00+00:00',\n"
            "    **metadata,\n"
            "}\n"
            "handle.seek(0)\n"
            "handle.truncate()\n"
            "json.dump(payload, handle)\n"
            "handle.flush()\n"
            "os.fsync(handle.fileno())\n"
            "time.sleep(60)\n",
            str(lock_path),
            str(workspace),
            json.dumps(metadata),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _wait_for_daemon_metadata(
    workspace: Path,
    *,
    timeout_seconds: float = 2.0,
    poll_interval_seconds: float = 0.02,
) -> DaemonRegistryEntry | None:
    workspace_obj = Workspace.from_path(workspace)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        entry = daemon_metadata_for_workspace(workspace_obj)
        if entry is not None:
            return entry
        time.sleep(poll_interval_seconds)
    return daemon_metadata_for_workspace(workspace_obj)


def test_register_and_unregister_daemon_uses_shared_lock_manager(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    workspace = tmp_path / "workspace"
    create_workspace(workspace)
    workspace_obj = Workspace.from_path(workspace)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    register_daemon_for_workspace(workspace_obj, pid=os.getpid(), log_dir=log_dir)

    entry = daemon_metadata_for_workspace(workspace_obj)
    assert entry is not None
    assert entry.status == "running"
    assert entry.pid == os.getpid()
    assert entry.log_dir == str(log_dir)
    assert daemon_lock_is_active_for_workspace(workspace_obj) is True
    assert get_workspace_daemon_for_workspace(workspace_obj) == entry

    unregister_daemon_for_workspace(workspace_obj, pid=os.getpid())

    assert daemon_metadata_for_workspace(workspace_obj) is None
    assert daemon_lock_is_active_for_workspace(workspace_obj) is False
    assert get_workspace_daemon_for_workspace(workspace_obj) is None
    assert daemon_lock_path(workspace).read_text(encoding="utf-8") == ""


def test_unregister_daemon_clears_stale_metadata_without_daemon_registry_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    workspace = tmp_path / "workspace"
    create_workspace(workspace)
    lock_path = daemon_lock_path(workspace)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "workspace": str(workspace.resolve()),
                "pid": 424242,
                "started_at": "2026-04-12T00:00:00Z",
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("litehive.daemon.registry.runner_pid_is_alive", lambda pid: False)

    unregister_daemon_for_workspace(Workspace.from_path(workspace), pid=424242)

    assert lock_path.read_text(encoding="utf-8") == ""
    assert not list((tmp_path / "data-home" / "litehive").glob("daemons.y*ml"))


def test_start_background_daemon_strips_agent_env(tmp_path: Path, monkeypatch) -> None:
    create_workspace(tmp_path)
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.setenv("LITEHIVE_STAGE", "implementing")
    monkeypatch.setenv("LITEHIVE_TASK_ID", "T-0446")
    monkeypatch.setattr("litehive.daemon.execution.daemon_metadata_for_workspace", lambda workspace: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon_for_workspace", lambda workspace: None)
    monkeypatch.setattr("litehive.daemon.execution.create_workspace_venvs_ready_for_workspace", lambda *args, **kwargs: None)

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
        "litehive.daemon.execution.get_workspace_daemon_for_workspace",
        lambda workspace: DaemonRegistryEntry(
            status="running",
            pid=4321,
            workspace=str(workspace.root),
            started_at=None,
            heartbeat_at=None,
            log_dir=None,
        ),
    )

    pid = start_background_daemon(tmp_path)

    assert pid == 4321
    kwargs_obj = captured["kwargs"]
    assert isinstance(kwargs_obj, dict)
    child_env = kwargs_obj["env"]
    assert "LITEHIVE_AGENT_ROLE" not in child_env
    assert "LITEHIVE_STAGE" not in child_env
    assert "LITEHIVE_TASK_ID" not in child_env
    assert child_env["LITEHIVE_WORKSPACE_ROOT"] == str(tmp_path.resolve())


def test_stop_workspace_daemon_escalates_to_sigkill_when_sigterm_ignored(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    create_workspace(
        workspace,
        LitehiveConfig(
            daemon=DaemonConfig(
                stop_grace_period_seconds=0.2,
                force_kill_timeout_seconds=2.0,
                exit_poll_interval_seconds=0.05,
            )
        ),
    )
    lock_path = daemon_lock_path(workspace)
    sleeper = _spawn_locked_daemon_like_process(
        workspace,
        {"heartbeat_at": "2026-04-12T00:00:00+00:00"},
    )
    try:
        entry = _wait_for_daemon_metadata(workspace)
        assert daemon_lock_is_active_for_workspace(Workspace.from_path(workspace)) is True
        assert entry is not None
        assert entry.status == "running"
        assert entry.pid == sleeper.pid
        started = time.monotonic()
        entry = stop_workspace_daemon(workspace)
        elapsed = time.monotonic() - started

        assert entry is not None
        assert entry.pid == sleeper.pid
        sleeper.wait(timeout=5)
        assert sleeper.returncode == -signal.SIGKILL
        assert elapsed < 3.0
        assert daemon_metadata_for_workspace(Workspace.from_path(workspace)) is None
        assert lock_path.read_text(encoding="utf-8") == ""
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)


@pytest.mark.parametrize(
    "lock_metadata",
    [
        pytest.param({"heartbeat_at": "2026-04-12T00:00:00+00:00"}, id="stale_heartbeat"),
        pytest.param({}, id="missing_heartbeat"),
        pytest.param({"heartbeat_at": "not-a-timestamp"}, id="invalid_heartbeat"),
    ],
)
def test_start_background_daemon_force_kills_unresponsive_live_daemon(
    tmp_path: Path,
    monkeypatch,
    lock_metadata: dict[str, str],
) -> None:
    workspace = tmp_path / "workspace"
    create_workspace(
        workspace,
        LitehiveConfig(
            daemon=DaemonConfig(
                force_kill_timeout_seconds=2.0,
                exit_poll_interval_seconds=0.05,
            )
        ),
    )
    lock_path = daemon_lock_path(workspace)
    sleeper = _spawn_locked_daemon_like_process(workspace, dict(lock_metadata))
    try:
        entry = _wait_for_daemon_metadata(workspace)
        assert entry is not None
        assert entry.status == "running"
        assert entry.pid == sleeper.pid
        monkeypatch.setattr("litehive.daemon.execution.create_workspace_venvs_ready_for_workspace", lambda *args, **kwargs: None)
        class FakeProcess:
            pid = 4321

            def poll(self) -> None:
                return None

        monkeypatch.setattr("litehive.daemon.execution.subprocess.Popen", lambda *args, **kwargs: FakeProcess())
        monkeypatch.setattr(
            "litehive.daemon.execution.get_workspace_daemon_for_workspace",
            lambda workspace: DaemonRegistryEntry(
                status="running",
                pid=4321,
                workspace=str(workspace.root),
                started_at=None,
                heartbeat_at=None,
                log_dir=None,
            ),
        )

        pid = start_background_daemon(workspace)

        assert pid == 4321
        sleeper.wait(timeout=5)
        assert sleeper.returncode == -signal.SIGKILL
        assert lock_path.read_text(encoding="utf-8") == ""
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)


def test_start_background_daemon_does_not_kill_live_pid_from_stale_metadata(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    create_workspace(workspace)
    lock_path = daemon_lock_path(workspace)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)\n"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        time.sleep(0.1)
        lock_path.write_text(
            json.dumps(
                {
                    "workspace": str(workspace.resolve()),
                    "pid": sleeper.pid,
                    "started_at": "2026-04-12T00:00:00+00:00",
                    "heartbeat_at": "2026-04-12T00:00:00+00:00",
                },
            ),
            encoding="utf-8",
        )
        workspace_obj = Workspace.from_path(workspace)
        assert daemon_lock_is_active_for_workspace(workspace_obj) is False
        entry = daemon_metadata_for_workspace(workspace_obj)
        assert entry is not None
        assert entry.status == "stale"
        assert entry.pid == sleeper.pid
        monkeypatch.setattr("litehive.daemon.execution.create_workspace_venvs_ready_for_workspace", lambda *args, **kwargs: None)

        class FakeProcess:
            pid = 4321

            def poll(self) -> None:
                return None

        monkeypatch.setattr("litehive.daemon.execution.subprocess.Popen", lambda *args, **kwargs: FakeProcess())
        monkeypatch.setattr(
            "litehive.daemon.execution.get_workspace_daemon_for_workspace",
            lambda workspace: DaemonRegistryEntry(
                status="running",
                pid=4321,
                workspace=str(workspace.root),
                started_at=None,
                heartbeat_at=None,
                log_dir=None,
            ),
        )

        pid = start_background_daemon(workspace)

        assert pid == 4321
        assert sleeper.poll() is None
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
