"""
Daemon process termination helpers.

This module owns the OS-level stop behavior for registered daemon
processes and child ``litehive run`` process groups. The daemon loop
decides when to stop; this module decides how to signal, wait, escalate,
and clear pid-guarded registry rows.
"""

import os
from pathlib import Path
import signal
import subprocess
import time

from litehive.config.model import DaemonConfig
from litehive.state.locking import runner_pid_is_alive

from .registry import unregister_daemon


def wait_for_pid_exit(pid: int, timeout_seconds: float, poll_interval_seconds: float) -> bool:
    """
    Block until a foreign pid is gone or the deadline passes.

    The stop sequence owns the registry row, not the ``Popen`` handle,
    so ``process.wait`` is unavailable and we poll
    ``runner_pid_is_alive`` instead. Used by SIGTERM/SIGKILL
    escalation before clearing the daemon registration.
    """
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        if not runner_pid_is_alive(pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, poll_interval_seconds))


def force_kill_recorded_daemon(workspace: Path, pid: int, config: DaemonConfig) -> None:
    """
    SIGKILL a daemon that ignored SIGTERM, then clear its registration.

    Reached either as the escalation step of
    ``terminate_recorded_daemon`` or directly from daemon startup when
    an existing registration row fails its heartbeat check. Raises if
    the process refuses to die so the caller does not falsely conclude
    the workspace is free for a new daemon to register.
    """
    if not runner_pid_is_alive(pid):
        unregister_daemon(workspace, pid=pid)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        unregister_daemon(workspace, pid=pid)
        return
    except PermissionError as exc:
        raise RuntimeError(f"failed to send SIGKILL to daemon pid={pid}: {exc}") from exc
    if not wait_for_pid_exit(
        pid,
        timeout_seconds=config.force_kill_timeout_seconds,
        poll_interval_seconds=config.exit_poll_interval_seconds,
    ):
        raise RuntimeError(f"daemon pid={pid} did not exit after SIGKILL")
    unregister_daemon(workspace, pid=pid)


def terminate_recorded_daemon(workspace: Path, pid: int, config: DaemonConfig) -> None:
    """
    Run the graceful-then-forceful stop sequence against a registered daemon.

    Sends SIGTERM first so the daemon's signal handler can flush its
    heartbeat thread and unregister cleanly; if the process is still
    alive after the configured daemon stop grace period we escalate to
    SIGKILL via ``force_kill_recorded_daemon``.
    """
    if not runner_pid_is_alive(pid):
        unregister_daemon(workspace, pid=pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        unregister_daemon(workspace, pid=pid)
        return
    except PermissionError as exc:
        raise RuntimeError(f"failed to send SIGTERM to daemon pid={pid}: {exc}") from exc
    if wait_for_pid_exit(
        pid,
        timeout_seconds=config.stop_grace_period_seconds,
        poll_interval_seconds=config.exit_poll_interval_seconds,
    ):
        unregister_daemon(workspace, pid=pid)
        return
    force_kill_recorded_daemon(workspace, pid=pid, config=config)


def terminate_child_process(process: subprocess.Popen[str]) -> None:
    """
    Forward a stop signal from the daemon to the whole ``litehive run`` subtree.

    The child runs in its own session (``start_new_session=True``)
    because it spawns subagents that themselves spawn subprocesses;
    sending SIGTERM only to the immediate child would orphan that
    subtree. ``killpg`` covers the whole group, with
    ``process.terminate`` as a fallback for platforms that lack
    ``killpg``.
    """
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
