"""
Filesystem paths for unified global Litehive state.

Provides three helpers used everywhere persistent state needs a
location: :func:`litehive_root` (the user-global directory),
:func:`workspace_data_dir` (per-workspace, keyed by hashed
absolute path), and :func:`workspace_path` (compose paths inside
the workspace dir). The XDG/``LITEHIVE_HOME`` override logic
lives only here so every caller honors it.
"""

import hashlib
import os
from pathlib import Path


def litehive_root() -> Path:
    """
    Resolve the global Litehive state directory.

    Honors ``LITEHIVE_HOME`` first (operator override), then
    ``XDG_DATA_HOME``, then falls back to
    ``~/.local/share/litehive``. The single source of truth for
    where workspace-id-keyed runtime data lives, so tests can
    redirect everything by setting one env var. Creates the
    directory eagerly so callers can write into it without an
    explicit ``mkdir``.
    """
    override = os.environ.get("LITEHIVE_HOME")
    if override:
        root = Path(override).expanduser()
    else:
        data_home = os.environ.get("XDG_DATA_HOME")
        if data_home:
            base = Path(data_home).expanduser()
        else:
            base = Path.home() / ".local" / "share"
        root = base / "litehive"
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_data_dir(root: Path) -> Path:
    """
    Map a workspace path to its stable hashed runtime directory.

    The hash isolates per-workspace state across all of a user's
    projects so two workspaces at different paths cannot scribble
    on each other's runtime files. Sixteen hex chars of SHA-256
    is plenty to avoid collisions in practice while keeping the
    directory name short.
    """
    canonical = str(root.expanduser().resolve()).encode("utf-8")
    workspace_id = hashlib.sha256(canonical).hexdigest()[:16]
    return litehive_root() / workspace_id


def workspace_path(root: Path, *parts: str) -> Path:
    """
    Compose a path inside a workspace's runtime directory.

    Every module that wants to write workspace-scoped state goes
    through here so paths stay aligned with the
    :func:`workspace_data_dir` layout. Variadic ``parts`` mirror
    :meth:`pathlib.Path.joinpath` so callers can build the path
    in one step.
    """
    return workspace_data_dir(root).joinpath(*parts)
