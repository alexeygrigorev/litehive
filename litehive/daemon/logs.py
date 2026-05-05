"""
Run-all session log directory management.

Each daemon iteration writes a per-session subdirectory under
``$workspace/logs/run-all/<UTC>``. This module owns "find the latest"
and "drop the oldest" so the daemon doesn't grow an unbounded log
tree and operators can land on the most recent session from one
``logs`` CLI invocation.
"""

import logging
from pathlib import Path
import shutil

from litehive.config.paths import workspace_path

logger = logging.getLogger(__name__)

_RUN_ALL_SESSION_RETENTION = 8


def latest_run_all_log_dir(workspace: Path) -> Path | None:
    """
    Return the most recent ``run-all`` session log directory.

    Called by the CLI ``logs`` subcommand and the daemon-side
    post-mortem helpers when no specific session is named — those
    paths want "the last thing that happened" without having to
    enumerate session names. Returns ``None`` for a workspace that
    has never run a daemon session.
    """
    workspace = workspace.resolve()
    log_base = workspace_path(workspace, "logs", "run-all")
    if not log_base.exists():
        return None
    candidates = sorted(path for path in log_base.iterdir() if path.is_dir())
    if candidates:
        return candidates[-1]
    return None


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
