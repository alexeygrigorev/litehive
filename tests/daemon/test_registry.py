import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import yaml
import pytest

from litehive.config.paths import litehive_root
from litehive.config.workspace import ensure_workspace
from litehive.daemon.execution import start_background_daemon, stop_workspace_daemon
from litehive.daemon.registry import (
    daemon_lock_is_active,
    daemon_lock_path,
    daemon_metadata,
    get_workspace_daemon,
    list_daemon_instances,
    register_daemon,
    unregister_daemon,
)


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
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "time.sleep(60)\n",
            str(lock_path),
            str(workspace),
            json.dumps(metadata),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
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


def test_stop_workspace_daemon_escalates_to_sigkill_when_sigterm_ignored(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    lock_path = daemon_lock_path(workspace)
    sleeper = _spawn_locked_daemon_like_process(
        workspace,
        {"heartbeat_at": "2026-04-12T00:00:00+00:00"},
    )
    try:
        time.sleep(0.1)
        assert daemon_lock_is_active(workspace) is True
        entry = daemon_metadata(workspace)
        assert entry is not None
        assert entry["status"] == "running"
        assert entry["pid"] == sleeper.pid
        monkeypatch.setattr("litehive.daemon.execution._DAEMON_STOP_GRACE_PERIOD_SECONDS", 0.2)
        monkeypatch.setattr("litehive.daemon.execution._DAEMON_FORCE_KILL_TIMEOUT_SECONDS", 2.0)
        monkeypatch.setattr("litehive.daemon.execution._DAEMON_EXIT_POLL_INTERVAL_SECONDS", 0.05)

        started = time.monotonic()
        entry = stop_workspace_daemon(workspace)
        elapsed = time.monotonic() - started

        assert entry is not None
        assert entry["pid"] == sleeper.pid
        sleeper.wait(timeout=5)
        assert sleeper.returncode == -signal.SIGKILL
        assert elapsed < 3.0
        assert daemon_metadata(workspace) is None
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
    ensure_workspace(workspace)
    lock_path = daemon_lock_path(workspace)
    sleeper = _spawn_locked_daemon_like_process(workspace, lock_metadata)
    try:
        time.sleep(0.1)
        entry = daemon_metadata(workspace)
        assert entry is not None
        assert entry["status"] == "running"
        assert entry["pid"] == sleeper.pid
        monkeypatch.setattr("litehive.daemon.execution._ensure_workspace_venvs_ready", lambda *args, **kwargs: None)
        monkeypatch.setattr("litehive.daemon.execution._DAEMON_FORCE_KILL_TIMEOUT_SECONDS", 2.0)
        monkeypatch.setattr("litehive.daemon.execution._DAEMON_EXIT_POLL_INTERVAL_SECONDS", 0.05)

        class FakeProcess:
            pid = 4321

            def poll(self) -> None:
                return None

        monkeypatch.setattr("litehive.daemon.execution.subprocess.Popen", lambda *args, **kwargs: FakeProcess())
        monkeypatch.setattr(
            "litehive.daemon.execution.get_workspace_daemon",
            lambda workspace: {"pid": 4321, "status": "running"},
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
    ensure_workspace(workspace)
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
            yaml.safe_dump(
                {
                    "workspace": str(workspace.resolve()),
                    "pid": sleeper.pid,
                    "started_at": "2026-04-12T00:00:00+00:00",
                    "heartbeat_at": "2026-04-12T00:00:00+00:00",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        assert daemon_lock_is_active(workspace) is False
        entry = daemon_metadata(workspace)
        assert entry is not None
        assert entry["status"] == "stale"
        assert entry["pid"] == sleeper.pid
        monkeypatch.setattr("litehive.daemon.execution._ensure_workspace_venvs_ready", lambda *args, **kwargs: None)

        class FakeProcess:
            pid = 4321

            def poll(self) -> None:
                return None

        monkeypatch.setattr("litehive.daemon.execution.subprocess.Popen", lambda *args, **kwargs: FakeProcess())
        monkeypatch.setattr(
            "litehive.daemon.execution.get_workspace_daemon",
            lambda workspace: {"pid": 4321, "status": "running"},
        )

        pid = start_background_daemon(workspace)

        assert pid == 4321
        assert sleeper.poll() is None
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
