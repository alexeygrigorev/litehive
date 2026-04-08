"""Tests for lock-free web dashboard snapshot."""

import os
import time

import yaml

from litehive.models import RunnerStatusState
from litehive.tasks.locking import runner_status_readonly


def _write_runner_lock(root, *, pid=None, heartbeat_at=None, started_at=None):
    lock_path = root / ".litehive" / ".runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    data = RunnerStatusState(
        status="running",
        pid=pid,
        workspace=str(root),
        started_at=started_at or "2026-01-01T00:00:00+00:00",
        heartbeat_at=heartbeat_at,
    )
    lock_path.write_text(yaml.safe_dump(data.model_dump(mode="python"), sort_keys=False))


def test_runner_status_readonly_returns_idle_when_no_metadata(tmp_path):
    (tmp_path / ".litehive").mkdir()
    result = runner_status_readonly(tmp_path)
    assert result.status == "idle"


def test_runner_status_readonly_returns_running_when_pid_alive(tmp_path):
    _write_runner_lock(tmp_path, pid=os.getpid())
    result = runner_status_readonly(tmp_path)
    assert result.status == "running"


def test_runner_status_readonly_returns_stale_when_pid_dead(tmp_path):
    _write_runner_lock(tmp_path, pid=999999999)
    result = runner_status_readonly(tmp_path)
    assert result.status == "stale"


def test_runner_status_readonly_returns_late_when_heartbeat_expired(tmp_path):
    _write_runner_lock(
        tmp_path,
        pid=os.getpid(),
        heartbeat_at="2020-01-01T00:00:00+00:00",
    )
    result = runner_status_readonly(tmp_path)
    assert result.status == "late"


def test_build_workspace_snapshot_does_not_block_on_runner_lock(tmp_path, monkeypatch):
    """Prove that build_workspace_snapshot never calls fcntl.flock."""
    import litehive.tasks as tasks_module

    # Set up minimal workspace state
    ws = tmp_path / ".litehive"
    ws.mkdir()
    state_file = ws / "state.yaml"
    state_file.write_text(yaml.safe_dump({"queue": [], "active_task_id": None}))
    (ws / "tasks").mkdir()

    # Write runner lock with our PID so readonly reports "running"
    _write_runner_lock(tmp_path, pid=os.getpid())

    # Monkeypatch fcntl.flock to always raise — if snapshot touches it, the test fails
    import fcntl as fcntl_mod

    original_flock = fcntl_mod.flock

    def flock_trap(fd, operation):
        raise BlockingIOError("flock should not be called by the snapshot path")

    monkeypatch.setattr(tasks_module, "fcntl", type("FakeFcntl", (), {
        "flock": staticmethod(flock_trap),
        "LOCK_EX": fcntl_mod.LOCK_EX,
        "LOCK_NB": fcntl_mod.LOCK_NB,
        "LOCK_UN": fcntl_mod.LOCK_UN,
    })())

    from litehive.web import build_workspace_snapshot

    start = time.monotonic()
    snapshot = build_workspace_snapshot(tmp_path)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0
    assert snapshot["runner"]["status"] == "running"
    assert isinstance(snapshot["tasks"], list)
