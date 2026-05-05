"""Process signaling helpers used by task stop / switch flows.

The code that decides *what* to do when a task is interrupted lives in
``tasks.status`` and its sibling transition modules. The mechanics of
"send SIGTERM, wait, escalate to SIGKILL, decide if the process is
zombie/dead" is just OS plumbing and lives here so the same helper is
shared by every flow that needs to kill a subagent.
"""

import os
import signal
import time
from pathlib import Path

from litehive.domain.task_ops import WorkspaceConflictError
from litehive.state.locking import runner_pid_is_alive


def terminate_subagent_pid(
    task_id: str,
    pid: int | None,
    wait_timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.1,
) -> bool:
    """Best-effort terminate a subagent process by pid.

    Returns ``True`` if a signal was sent to a live process, ``False``
    if the pid was already dead or ``None``. Raises
    :class:`WorkspaceConflictError` if SIGTERM and then SIGKILL both
    fail to reap the process within the timeout (operator must
    intervene; we don't silently leave a zombie running).
    """

    def _pid_is_dead() -> bool:
        """Detect when a subagent pid has actually exited (or become a zombie); checks ``waitpid``, ``/proc/<pid>/status`` State, and the runner-lock liveness probe in turn so flaky single-source signals don't make ``terminate_subagent_pid`` spin."""
        if pid is None:
            return True
        try:
            reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            reaped_pid = 0
        if reaped_pid == pid:
            return True
        proc_status = Path(f"/proc/{pid}/status")
        if proc_status.exists():
            try:
                for line in proc_status.read_text(encoding="utf-8").splitlines():
                    if line.startswith("State:"):
                        return "\tZ" in line or " zombie" in line
            except OSError:
                pass
        return not runner_pid_is_alive(pid)

    if pid is None or _pid_is_dead():
        return False

    sleep_interval = max(poll_interval_seconds, 0.01)

    def _wait_until_dead(timeout_seconds: float) -> bool:
        """Poll ``_pid_is_dead`` until the process exits or the deadline passes; the SIGTERM/SIGKILL escalation flow needs a bounded wait between signals so a stuck subagent cannot block stop forever."""
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while not _pid_is_dead() and time.monotonic() < deadline:
            time.sleep(sleep_interval)
        return _pid_is_dead()

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False

    if _wait_until_dead(wait_timeout_seconds):
        return True

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True

    if _wait_until_dead(wait_timeout_seconds):
        return True

    raise WorkspaceConflictError(f"subagent pid {pid} for task {task_id} did not exit after SIGTERM/SIGKILL")
