"""Log directory management for daemon sessions."""

import logging
from pathlib import Path
import shutil

from litehive.config.paths import workspace_path

logger = logging.getLogger(__name__)

_RUN_ALL_SESSION_RETENTION = 8


def latest_run_all_log_dir(workspace: Path) -> Path | None:
    """Return the most recent ``run-all`` session log directory; called by the CLI ``logs`` subcommand and post-mortem helpers when no specific session is named."""
    workspace = workspace.resolve()
    log_base = workspace_path(workspace, "logs", "run-all")
    if not log_base.exists():
        return None
    candidates = sorted(path for path in log_base.iterdir() if path.is_dir())
    if candidates:
        return candidates[-1]
    return None


def prune_run_all_log_dirs(log_base: Path, keep: int = _RUN_ALL_SESSION_RETENTION) -> None:
    """Drop oldest run-all session directories so the log tree doesn't grow unbounded; called by the daemon when starting a new run-all session."""
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
    """Pick the most recent log file in a session matching ``pattern``; used by ``logs`` to surface specific stage logs without scanning the whole tree."""
    if log_dir is None or not log_dir.exists():
        return None
    matches = sorted(log_dir.glob(pattern))
    if matches:
        return matches[-1]
    return None
