"""Global cross-workspace registry backed by a locked YAML file."""

from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import logging
import os
from pathlib import Path
import threading
import time
from typing import Iterator

import yaml

from litehive.config.paths import litehive_config_root

log = logging.getLogger(__name__)

_REGISTRY_MUTEX = threading.RLock()

_DEFAULT_BUSY_TIMEOUT_MS = 30_000
_DEFAULT_LOCK_RETRIES = 0
_DEFAULT_LOCK_RETRY_DELAY_MS = 100


class _RegistryCorruptError(RuntimeError):
    """Raised when the registry YAML cannot be parsed into the expected shape."""


def workspace_registry_path() -> Path:
    return litehive_config_root() / "workspaces.yaml"


def _registry_path() -> Path:
    return workspace_registry_path()


def _registry_lock_path() -> Path:
    return _registry_path().with_name(".workspaces.lock")


def _close_all_registry_connections() -> None:
    """Compatibility shim retained for older tests and callers."""


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


def _registry_lock_retries() -> int:
    return _int_env("LITEHIVE_REGISTRY_LOCK_RETRIES", _DEFAULT_LOCK_RETRIES)


def _registry_busy_timeout_seconds() -> float:
    return max(_int_env("LITEHIVE_REGISTRY_BUSY_TIMEOUT_MS", _DEFAULT_BUSY_TIMEOUT_MS), 1) / 1000


def _registry_lock_retry_delay_seconds() -> float:
    return _int_env("LITEHIVE_REGISTRY_LOCK_RETRY_DELAY_MS", _DEFAULT_LOCK_RETRY_DELAY_MS) / 1000


@contextmanager
def _locked_registry_handle() -> Iterator[None]:
    lock_path = _registry_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    retries_remaining = _registry_lock_retries()
    busy_timeout_seconds = _registry_busy_timeout_seconds()
    retry_delay_seconds = _registry_lock_retry_delay_seconds()
    poll_interval_seconds = retry_delay_seconds or 0.01
    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            deadline = time.monotonic() + busy_timeout_seconds
            acquired = False
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    sleep_for = min(poll_interval_seconds, remaining)
                    if sleep_for > 0:
                        time.sleep(sleep_for)
            if acquired:
                break
            if retries_remaining <= 0:
                raise TimeoutError(f"workspace registry remained locked: {lock_path}") from None
            retries_remaining -= 1
            if retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_registry_entries(path: Path) -> list[Path]:
    if not path.exists():
        return []
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError as exc:
        raise _RegistryCorruptError(str(exc)) from exc
    except OSError as exc:
        raise OSError(f"failed to read workspace registry {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise _RegistryCorruptError("workspace registry must contain a list of workspace paths")

    if any(not isinstance(entry, str) for entry in payload):
        raise _RegistryCorruptError("workspace registry must contain only string workspace paths")

    roots: list[Path] = []
    seen: set[Path] = set()
    for entry in payload:
        try:
            resolved = Path(entry).expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def _write_registry_entries(path: Path, roots: list[Path]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump([str(root) for root in roots], sort_keys=False),
        encoding="utf-8",
    )


def _backup_corrupt_registry(path: Path, reason: str) -> None:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.corrupt-{timestamp}")
    try:
        path.replace(backup)
    except OSError as exc:
        log.warning("failed to move corrupt workspace registry %s aside (%s)", path, exc)
        return
    log.warning("workspace registry %s was corrupt (%s); moved it to %s", path, reason, backup)


def list_registered_workspace_paths() -> list[Path]:
    path = _registry_path()
    with _REGISTRY_MUTEX:
        try:
            with _locked_registry_handle():
                return _load_registry_entries(path)
        except TimeoutError as exc:
            log.warning("%s", exc)
            return []
        except _RegistryCorruptError as exc:
            _backup_corrupt_registry(path, str(exc))
            return []
        except OSError as exc:
            log.warning("%s", exc)
            return []


def register_workspace_path(root: Path) -> None:
    resolved = root.expanduser().resolve()
    path = _registry_path()
    with _REGISTRY_MUTEX:
        try:
            with _locked_registry_handle():
                try:
                    roots = _load_registry_entries(path)
                except _RegistryCorruptError as exc:
                    _backup_corrupt_registry(path, str(exc))
                    roots = []
                roots = [resolved, *[candidate for candidate in roots if candidate != resolved]]
                _write_registry_entries(path, roots)
        except TimeoutError as exc:
            log.warning("%s", exc)
        except OSError as exc:
            log.warning("failed to update workspace registry %s (%s)", path, exc)
