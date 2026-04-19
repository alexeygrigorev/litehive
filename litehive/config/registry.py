"""Global cross-workspace registry backed by a YAML path list."""

from contextlib import contextmanager
import logging
from pathlib import Path

import yaml

from litehive.config.paths import workspace_registry_path

try:  # pragma: no cover - Windows fallback
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

log = logging.getLogger(__name__)


@contextmanager
def _locked_registry_file():
    registry_path = workspace_registry_path()
    lock_path = registry_path.with_suffix(registry_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield registry_path
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_registry_entries(path: Path) -> list[Path]:
    if not path.exists():
        return []
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError) as exc:
        log.warning("workspace registry file %s is unreadable (%s); continuing empty", path, exc)
        return []
    if not isinstance(payload, list):
        log.warning("workspace registry file %s must contain a list of workspace paths; continuing empty", path)
        return []

    roots: list[Path] = []
    seen: set[Path] = set()
    for entry in payload:
        if not isinstance(entry, str):
            continue
        resolved = Path(entry).expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def list_registered_workspace_paths() -> list[Path]:
    with _locked_registry_file() as registry_path:
        return _load_registry_entries(registry_path)


def register_workspace_path(root: Path) -> None:
    resolved = root.expanduser().resolve()
    with _locked_registry_file() as registry_path:
        existing = [path for path in _load_registry_entries(registry_path) if path != resolved]
        payload = [str(path) for path in [resolved, *existing]]
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = registry_path.with_suffix(registry_path.suffix + ".tmp")
        temp_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        temp_path.replace(registry_path)
