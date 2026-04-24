"""Helpers for explicit directory-tree cleanup."""

from __future__ import annotations

import logging
from pathlib import Path
import shutil


def remove_tree_logged(path: Path, *, logger: logging.Logger, target_label: str) -> None:
    logger.info("Deleting %s %s", target_label, path)
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    except OSError as exc:
        logger.exception("Failed to delete %s %s", target_label, path)
        raise OSError(f"failed to delete {target_label} {path}: {exc}") from exc
