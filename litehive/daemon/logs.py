"""Log directory management for daemon sessions."""

import logging
from pathlib import Path
import shutil

from litehive.config.paths import workspace_path

logger = logging.getLogger(__name__)

_RUN_ALL_SESSION_RETENTION = 8


def latest_run_all_log_dir(workspace: Path) -> Path | None:
    workspace = workspace.resolve()
    log_base = workspace_path(workspace, "logs", "run-all")
    if not log_base.exists():
        return None
    candidates = sorted(path for path in log_base.iterdir() if path.is_dir())
    return candidates[-1] if candidates else None


def prune_run_all_log_dirs(log_base: Path, keep: int = _RUN_ALL_SESSION_RETENTION) -> None:
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
    if log_dir is None or not log_dir.exists():
        return None
    matches = sorted(log_dir.glob(pattern))
    return matches[-1] if matches else None
