"""Filesystem paths for unified global Litehive state."""

import hashlib
import os
from pathlib import Path


def litehive_root() -> Path:
    """Resolve the global Litehive state directory honoring ``LITEHIVE_HOME`` and ``XDG_DATA_HOME``; the single source of truth for where workspace-id-keyed runtime data lives."""
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
    """Map a workspace path to its stable hashed runtime directory under ``litehive_root``; the hash isolates per-workspace state across all of a user's projects."""
    canonical = str(root.expanduser().resolve()).encode("utf-8")
    workspace_id = hashlib.sha256(canonical).hexdigest()[:16]
    return litehive_root() / workspace_id


def workspace_path(root: Path, *parts: str) -> Path:
    """Compose a path inside a workspace's runtime directory; every module that wants to write workspace-scoped state goes through here so paths stay aligned."""
    return workspace_data_dir(root).joinpath(*parts)
