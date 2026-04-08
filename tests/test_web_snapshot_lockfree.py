"""Tests for lock-free web dashboard snapshot reads."""

import os
import time
from pathlib import Path

import yaml

from tests.workspace_helpers import *  # noqa: F401,F403

from litehive.tasks import runner_status_readonly
from litehive.tasks.paths import runner_lock_path


def _write_runner_lock_metadata(root: Path, data: dict) -> None:
    lock_path = runner_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_runner_status_readonly_returns_idle_when_no_metadata(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    status = runner_status_readonly(tmp_path)
    assert status.status == "idle"
    assert status.pid is None


def test_runner_status_readonly_returns_running_when_pid_alive(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    # Use our own PID — guaranteed alive.
    _write_runner_lock_metadata(tmp_path, {
        "status": "running",
        "pid": os.getpid(),
        "workspace": str(tmp_path),
        "command": "litehive run",
        "started_at": now,
        "heartbeat_at": now,
    })
    status = runner_status_readonly(tmp_path)
    assert status.status == "running"
    assert status.pid == os.getpid()


def test_runner_status_readonly_returns_stale_when_pid_dead(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    # Use a PID that almost certainly doesn't exist.
    _write_runner_lock_metadata(tmp_path, {
        "status": "running",
        "pid": 2_000_000_000,
        "workspace": str(tmp_path),
        "command": "litehive run",
        "started_at": "2026-04-08T10:00:00+00:00",
        "heartbeat_at": "2026-04-08T10:00:05+00:00",
    })
    status = runner_status_readonly(tmp_path)
    assert status.status == "stale"


def test_runner_status_readonly_returns_late_when_heartbeat_expired(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    # Heartbeat far in the past but PID alive.
    _write_runner_lock_metadata(tmp_path, {
        "status": "running",
        "pid": os.getpid(),
        "workspace": str(tmp_path),
        "command": "litehive run",
        "started_at": "2020-01-01T00:00:00+00:00",
        "heartbeat_at": "2020-01-01T00:00:00+00:00",
    })
    status = runner_status_readonly(tmp_path)
    assert status.status == "late"


def test_build_workspace_snapshot_does_not_block_on_runner_lock(tmp_path: Path, monkeypatch) -> None:
    """Snapshot must complete even when fcntl.flock would block (simulated)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Running task")
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = [task.id]
    save_state(tmp_path, state)

    # Write runner metadata with our own PID so readonly reports "running".
    _write_runner_lock_metadata(tmp_path, {
        "status": "running",
        "pid": os.getpid(),
        "workspace": str(tmp_path),
        "command": "litehive run --drain",
        "started_at": now,
        "heartbeat_at": now,
    })

    # Make fcntl.flock always raise BlockingIOError to prove the snapshot
    # code path never calls it.
    import litehive.tasks.locking as locking_mod

    def flock_that_blocks(fd, flags):
        raise BlockingIOError("flock must not be called from snapshot path")

    monkeypatch.setattr(locking_mod, "fcntl", type("FakeFcntl", (), {
        "flock": staticmethod(flock_that_blocks),
        "LOCK_EX": 2,
        "LOCK_NB": 4,
        "LOCK_UN": 8,
    })())

    start = time.monotonic()
    snapshot = build_workspace_snapshot(tmp_path)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"Snapshot took {elapsed:.1f}s — should be near-instant"
    assert snapshot["active_task_id"] == task.id
    assert snapshot["runner"]["status"] == "running"
    assert snapshot["runner"]["pid"] == os.getpid()
    assert len(snapshot["tasks"]) >= 1
