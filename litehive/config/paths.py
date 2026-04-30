"""Filesystem paths for unified global Litehive state."""

import hashlib
import os
from pathlib import Path


def litehive_root() -> Path:
    override = os.environ.get("LITEHIVE_HOME")
    if override:
        root = Path(override).expanduser()
    else:
        data_home = os.environ.get("XDG_DATA_HOME")
        base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
        root = base / "litehive"
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_data_dir(root: Path) -> Path:
    canonical = str(root.expanduser().resolve()).encode("utf-8")
    workspace_id = hashlib.sha256(canonical).hexdigest()[:16]
    return litehive_root() / workspace_id


def workspace_path(root: Path, *parts: str) -> Path:
    return workspace_data_dir(root).joinpath(*parts)
