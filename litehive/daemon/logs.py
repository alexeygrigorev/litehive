"""
Run-all session log directory management.

Each daemon iteration writes a per-session subdirectory under
``$workspace/logs/run-all/<UTC>``. This module owns "find the latest"
and "drop the oldest" so the daemon doesn't grow an unbounded log
tree and operators can land on the most recent session from one
``logs`` CLI invocation.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import shutil

from litehive.workspace import Workspace

logger = logging.getLogger(__name__)

_RUN_ALL_SESSION_RETENTION = 8


@dataclass(frozen=True, slots=True)
class DaemonLogs:
    """
    Run-all log path helper bound to one workspace.

    The daemon executor uses this object to prepare a session
    directory, while CLI/status wrappers use it to locate the latest
    run-all session or matching log file without recomputing runtime
    paths in multiple modules.
    """

    workspace: Workspace

    def run_all_base(self) -> Path:
        return self.workspace.runtime_path("logs", "run-all")

    def prepare_session(self, session_dir: Path | None = None) -> Path:
        log_base = self.run_all_base()
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        log_root = session_dir or (log_base / timestamp)
        log_root.mkdir(parents=True, exist_ok=True)
        self.prune_sessions()
        return log_root

    def prune_sessions(self, keep: int = _RUN_ALL_SESSION_RETENTION) -> None:
        prune_run_all_log_dirs(self.run_all_base(), keep=keep)

    def latest_run_all_dir(self) -> Path | None:
        log_base = self.run_all_base()
        if not log_base.exists():
            return None
        candidates = sorted(path for path in log_base.iterdir() if path.is_dir())
        if candidates:
            return candidates[-1]
        return None

    def latest_matching(self, pattern: str) -> Path | None:
        return latest_matching(self.latest_run_all_dir(), pattern)


def prune_run_all_log_dirs(log_base: Path, keep: int = _RUN_ALL_SESSION_RETENTION) -> None:
    """
    Drop the oldest run-all session directories so the log tree stays bounded.

    Called by the daemon when starting a new run-all session, so
    pruning is amortized across iterations rather than running on a
    timer. The retention default (``_RUN_ALL_SESSION_RETENTION``) is
    high enough to keep a useful debugging history but low enough to
    bound disk for a long-running daemon.
    """
    if not log_base.exists():
        return
    directories = sorted(path for path in log_base.iterdir() if path.is_dir())
    for directory in directories[:-keep]:
        try:
            logger.info("Pruning log dir %s", directory)
            shutil.rmtree(directory)
        except OSError as exc:
            logger.exception("Failed to prune log dir %s", directory)
            raise OSError(f"failed to prune log dir {directory}: {exc}") from exc


def latest_matching(log_dir: Path | None, pattern: str) -> Path | None:
    """
    Pick the most recent log file in a session matching ``pattern``.

    Used by the ``logs`` CLI to surface specific stage or run logs
    inside a session (``*-run.log``, ``*-stage-*.log``) without
    forcing the operator to know the exact filename. Returns ``None``
    for missing directories or empty matches so the caller can
    distinguish "no log yet" from a hard failure.
    """
    if log_dir is None or not log_dir.exists():
        return None
    matches = sorted(log_dir.glob(pattern))
    if matches:
        return matches[-1]
    return None
