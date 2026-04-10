"""State load/save and atomic write helpers."""

import gzip
import logging
import os
from pathlib import Path

import yaml

from litehive.config import ensure_workspace, state_path
from litehive.models import WorkspaceState
from litehive.storage import runtime_store

from .constants import _MISSING

logger = logging.getLogger(__name__)


def load_state(root: Path) -> WorkspaceState:
    ensure_workspace(root)
    store = runtime_store(root)
    state = store.load_workspace_state()
    legacy_path = state_path(root)
    if legacy_path.exists():
        legacy = WorkspaceState(
            **(yaml.safe_load(legacy_path.read_text(encoding="utf-8")) or {})
        )
        if state is None or state == WorkspaceState():
            store.save_workspace_state(legacy)
            return legacy
        return state
    if state is None:
        state = WorkspaceState()
        store.save_workspace_state(state)
    return state


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            if not os.environ.get("LITEHIVE_SKIP_FSYNC"):
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _atomic_write_gzip_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with gzip.open(temp_path, "wt", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_atomic_files(writes: dict[Path, str]) -> None:
    _write = _atomic_write_text
    snapshots = {
        path: path.read_text(encoding="utf-8") if path.exists() else _MISSING for path in writes
    }
    applied: list[Path] = []
    try:
        for path, content in writes.items():
            _write(path, content)
            applied.append(path)
    except Exception:
        for path in reversed(applied):
            previous = snapshots[path]
            if previous is _MISSING:
                if path.exists():
                    path.unlink()
                continue
            _write(path, previous)
        raise


def _serialize_state(state: WorkspaceState) -> str:
    return yaml.safe_dump(state.model_dump(mode="python"), sort_keys=False)


def save_state(root: Path, state: WorkspaceState) -> None:
    from litehive.workspace.locking import workspace_mutation_guard

    with workspace_mutation_guard(root):
        runtime_store(root).save_workspace_state(state)
        _atomic_write_text(state_path(root), _serialize_state(state))


def _save_state_without_runner_guard(root: Path, state: WorkspaceState) -> None:
    runtime_store(root).save_workspace_state(state)
    _atomic_write_text(state_path(root), _serialize_state(state))


def set_pool_stop_reason(root: Path, stop_reason: str | None) -> WorkspaceState:
    from litehive.workspace.locking import _workspace_lock

    with _workspace_lock(root):
        state = load_state(root)
        state.pool_stop_reason = stop_reason
        save_state(root, state)
        return state
