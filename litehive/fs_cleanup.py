"""
Sanctioned filesystem-removal helper for tree/file/symlink targets.

Worktree cleanup, log pruning, recovery, and venv-link replacement
all need the same shape: log before/after, surface failures as
``OSError``, never silently swallow. Routing every removal through
this helper keeps the operator log consistent and ensures one place
to change if (e.g.) we want to add retry-on-EBUSY semantics later.
"""

import logging
from pathlib import Path
import shutil


def remove_tree_logged(path: Path, logger: logging.Logger, target_label: str) -> None:
    """
    Delete a file, symlink, or directory tree with structured logging on both sides.

    The only sanctioned filesystem-removal helper in the project,
    so worktree cleanup, log pruning, and recovery all surface
    failures the same way. Wraps file/symlink unlink and tree
    rmtree behind a single API; raises ``OSError`` on failure
    rather than swallowing because losing track of why a deletion
    failed makes recovery much harder.
    """
    logger.info("Deleting %s %s", target_label, path)
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    except OSError as exc:
        logger.exception("Failed to delete %s %s", target_label, path)
        raise OSError(f"failed to delete {target_label} {path}: {exc}") from exc
