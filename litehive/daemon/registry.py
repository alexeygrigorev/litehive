"""Global daemon registry with file locking."""

from contextlib import contextmanager
import fcntl
import logging
import os
from pathlib import Path

import yaml

from litehive.config.paths import daemon_registry_path
from litehive.domain.common import utcnow

logger = logging.getLogger(__name__)


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _empty_registry() -> dict[str, object]:
    return {"daemons": {}}


@contextmanager
def _locked_registry() -> dict[str, object]:
    path = daemon_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read()
        data = yaml.safe_load(raw) if raw.strip() else _empty_registry()
        if not isinstance(data, dict):
            data = _empty_registry()
        daemons = data.get("daemons")
        if not isinstance(daemons, dict):
            data["daemons"] = {}
        _prune_registry_in_place(data)
        yield data
        handle.seek(0)
        handle.truncate()
        yaml.safe_dump(data, handle, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _prune_registry_in_place(data: dict[str, object]) -> None:
    daemons = data.setdefault("daemons", {})
    if not isinstance(daemons, dict):
        data["daemons"] = {}
        return
    stale: list[str] = []
    for workspace, payload in daemons.items():
        if not isinstance(payload, dict):
            stale.append(workspace)
            continue
        pid = payload.get("pid")
        if not isinstance(pid, int) or not pid_is_alive(pid):
            stale.append(workspace)
    for workspace in stale:
        daemons.pop(workspace, None)


def list_daemon_instances() -> list[dict[str, object]]:
    with _locked_registry() as data:
        daemons = data.get("daemons", {})
        if not isinstance(daemons, dict):
            return []
        return [dict(payload) for _, payload in sorted(daemons.items()) if isinstance(payload, dict)]


def get_workspace_daemon(workspace: Path) -> dict[str, object] | None:
    workspace = str(workspace.resolve())
    with _locked_registry() as data:
        daemons = data.get("daemons", {})
        if not isinstance(daemons, dict):
            return None
        payload = daemons.get(workspace)
        return dict(payload) if isinstance(payload, dict) else None


def register_daemon(workspace: Path, *, pid: int, log_dir: Path) -> None:
    workspace = workspace.resolve()
    workspace_key = str(workspace)
    with _locked_registry() as data:
        daemons = data.setdefault("daemons", {})
        assert isinstance(daemons, dict)
        existing = daemons.get(workspace_key)
        if isinstance(existing, dict):
            existing_pid = existing.get("pid")
            if isinstance(existing_pid, int) and existing_pid != pid and pid_is_alive(existing_pid):
                raise RuntimeError(f"daemon already running for {workspace_key}: pid={existing_pid}")
        daemons[workspace_key] = {
            "workspace": workspace_key,
            "pid": pid,
            "started_at": utcnow(),
            "log_dir": str(log_dir),
        }


def unregister_daemon(workspace: Path, *, pid: int | None = None) -> None:
    workspace_key = str(workspace.resolve())
    with _locked_registry() as data:
        daemons = data.setdefault("daemons", {})
        assert isinstance(daemons, dict)
        existing = daemons.get(workspace_key)
        if not isinstance(existing, dict):
            return
        if pid is not None and existing.get("pid") != pid:
            return
        daemons.pop(workspace_key, None)
