"""State load/save and atomic write helpers."""

import gzip
import logging
import os
from pathlib import Path

import yaml

from litehive.config.paths import state_path
from litehive.config.workspace import ensure_workspace
from litehive.models.task_models import WorkspaceState
from litehive.state.store import runtime_store

from .constants import MISSING

logger = logging.getLogger(__name__)


def load_state(root: Path) -> WorkspaceState:
    ensure_workspace(root)
    store = runtime_store(root)
    state = store.load_workspace_state()
    if state is None:
        state = WorkspaceState()
        store.save_workspace_state(state)
    return state


def atomic_write_text(path: Path, content: str) -> None:
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


def atomic_write_gzip_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with gzip.open(temp_path, "wt", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_atomic_files(writes: dict[Path, str]) -> None:
    snapshots = {
        path: path.read_text(encoding="utf-8") if path.exists() else MISSING for path in writes
    }
    applied: list[Path] = []
    try:
        for path, content in writes.items():
            atomic_write_text(path, content)
            applied.append(path)
    except Exception:
        for path in reversed(applied):
            previous = snapshots[path]
            if previous is MISSING:
                if path.exists():
                    path.unlink()
                continue
            atomic_write_text(path, previous)
        raise


def write_atomic_files_and_then(writes: dict[Path, str], callback) -> None:
    snapshots = {
        path: path.read_text(encoding="utf-8") if path.exists() else MISSING for path in writes
    }
    applied: list[Path] = []
    try:
        for path, content in writes.items():
            atomic_write_text(path, content)
            applied.append(path)
        callback()
    except Exception:
        for path in reversed(applied):
            previous = snapshots[path]
            if previous is MISSING:
                if path.exists():
                    path.unlink()
                continue
            atomic_write_text(path, previous)
        raise


def serialize_state(state: WorkspaceState) -> str:
    return yaml.safe_dump(state.model_dump(mode="python"), sort_keys=False)


def save_state(root: Path, state: WorkspaceState) -> None:
    from litehive.state.locking import workspace_mutation_guard

    with workspace_mutation_guard(root):
        runtime_store(root).save_workspace_state(state)
        atomic_write_text(state_path(root), serialize_state(state))


def save_state_without_runner_guard(root: Path, state: WorkspaceState) -> None:
    runtime_store(root).save_workspace_state(state)
    atomic_write_text(state_path(root), serialize_state(state))


def set_pool_stop_reason(root: Path, stop_reason: str | None) -> WorkspaceState:
    from litehive.state.locking import workspace_lock

    with workspace_lock(root):
        state = load_state(root)
        state.pool_stop_reason = stop_reason
        save_state(root, state)
        return state
