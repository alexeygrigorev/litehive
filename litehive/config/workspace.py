"""Workspace bootstrap helpers."""

import fcntl
import logging
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

import yaml

from litehive.config.model import LitehiveConfig
from litehive.config.paths import (
    config_path,
    context_path,
    workspace_database_path,
    workspace_registry_path,
    workspace_dir,
    workspace_gitignore_path,
)
from litehive.config.profiles import render_context_template

log = logging.getLogger(__name__)


def render_workspace_gitignore() -> str:
    return "\n".join(
        [
            "# Transient litehive runtime state",
            ".lock",
            ".runner.lock",
            "logs/",
            "pool-summary.txt",
            "engine-monitoring.yaml",
            "worktrees/",
            "tasks/*/runtime.yaml",
            "tasks/*/reports/commit_to_git-*.yaml",
            "",
        ]
    )


def _resolve_workspace_root(path: Path) -> Path:
    """Resolve back to the main workspace root if path is inside a worktree."""
    parts = path.resolve().parts
    for i, part in enumerate(parts):
        if part == ".litehive" and i + 2 < len(parts) and parts[i + 1] == "worktrees":
            return Path(*parts[:i])
    return path.resolve()


def _reject_invalid_workspace_path(path: Path | str, *, source: str) -> None:
    raw = str(path).strip()
    if raw.startswith("$"):
        raise ValueError(
            f"invalid workspace root from {source}: {raw!r} starts with '$' and looks like an unresolved shell variable"
        )


def _nested_litehive_ancestor(path: Path) -> Path | None:
    for ancestor in path.parents:
        if ancestor.name == ".litehive":
            return ancestor
    return None


def _validate_workspace_root(
    root: Path,
    *,
    source: str,
    allow_worktree_root_alias: bool = True,
) -> Path:
    _reject_invalid_workspace_path(root, source=source)
    expanded = Path(root).expanduser()
    resolved_input = expanded.resolve()
    nested_ancestor = _nested_litehive_ancestor(resolved_input)
    if nested_ancestor is not None and not allow_worktree_root_alias:
        raise ValueError(
            f"invalid workspace root from {source}: {resolved_input} is nested inside another .litehive tree"
        )
    resolved_root = _resolve_workspace_root(expanded)
    if _nested_litehive_ancestor(resolved_root) is not None:
        raise ValueError(
            f"invalid workspace root from {source}: {resolved_root} is nested inside another .litehive tree"
        )
    return resolved_root


def _task_exists(root: Path, task_id: str) -> bool:
    tasks_root = workspace_dir(root) / "tasks"
    return any(tasks_root.glob(f"{task_id}-*"))


def _registered_workspace_roots() -> list[Path]:
    registry_path = workspace_registry_path()
    if not registry_path.exists():
        return []
    try:
        data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or []
    except (yaml.YAMLError, OSError) as exc:
        log.warning("workspaces registry %s was unreadable (%s); skipping", registry_path, exc)
        return []
    if not isinstance(data, list):
        return []
    roots: list[Path] = []
    seen: set[Path] = set()
    for entry in data:
        if not isinstance(entry, str):
            continue
        try:
            resolved = _validate_workspace_root(Path(entry), source=str(registry_path))
        except ValueError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)
    return roots


def resolve_workspace(
    task_id: str | None,
    *,
    workspace: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    effective_task_id = task_id or os.environ.get("LITEHIVE_TASK_ID")
    if workspace is not None:
        resolved = _validate_workspace_root(workspace, source="--workspace")
        _register_workspace(resolved)
        return resolved

    env_workspace = os.environ.get("LITEHIVE_WORKSPACE_ROOT")
    if env_workspace:
        resolved_env_workspace = _validate_workspace_root(
            Path(env_workspace), source="LITEHIVE_WORKSPACE_ROOT"
        )
        if not effective_task_id or _task_exists(resolved_env_workspace, effective_task_id):
            _register_workspace(resolved_env_workspace)
            return resolved_env_workspace

    search_root = (cwd or Path.cwd()).resolve()
    for candidate in (search_root, *search_root.parents):
        if not workspace_dir(candidate).is_dir():
            continue
        resolved = _validate_workspace_root(candidate, source=f"cwd:{search_root}")
        if effective_task_id and not _task_exists(resolved, effective_task_id):
            continue
        _register_workspace(resolved)
        return resolved

    if effective_task_id:
        for root in _registered_workspace_roots():
            if _task_exists(root, effective_task_id):
                _register_workspace(root)
                return root

    raise ValueError(
        "unable to resolve workspace: provide --workspace, set LITEHIVE_WORKSPACE_ROOT, "
        "run inside a Litehive workspace, or set LITEHIVE_TASK_ID so the workspace registry can be used"
    )


def _register_workspace(root: Path) -> None:
    # Tests don't need the user-global registry. Skipping the read-modify-write
    # cycle per test avoids ~0.5s of lock+yaml overhead per ensure_workspace call
    # (the main reason the pytest suite grew from 2min to 15min after T-0291).
    if os.environ.get("LITEHIVE_SKIP_REGISTRY"):
        return
    registry_path = workspace_registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = registry_path.with_suffix(registry_path.suffix + ".lock")
    # fcntl.flock serializes concurrent writers across processes (e.g. multiple
    # pytest workers + a running daemon). Without this, read-modify-write races
    # truncate the yaml mid-flight and crash every future reader.
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if registry_path.exists():
                try:
                    raw = registry_path.read_text(encoding="utf-8")
                    existing = yaml.safe_load(raw) or []
                except (yaml.YAMLError, OSError) as exc:
                    # Registry is a cache — losing it is fine. Log and rebuild.
                    log.warning(
                        "workspaces registry %s was unreadable (%s); rebuilding",
                        registry_path,
                        exc,
                    )
                    existing = []
            else:
                existing = []
            if not isinstance(existing, list):
                existing = []
            workspaces = {str(root.resolve())}
            for entry in existing:
                if not isinstance(entry, str):
                    continue
                try:
                    workspaces.add(
                        str(_validate_workspace_root(Path(entry), source=str(registry_path)))
                    )
                except ValueError:
                    continue
            # Atomic write: tmp file in same dir + os.replace. No partial files,
            # no reader ever sees a truncated document.
            payload = yaml.safe_dump(sorted(workspaces), sort_keys=False)
            tmp_fd, tmp_name = tempfile.mkstemp(
                prefix=".workspaces.yaml.",
                suffix=".tmp",
                dir=str(registry_path.parent),
            )
            try:
                skip_fsync = bool(os.environ.get("LITEHIVE_SKIP_FSYNC"))
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_file:
                    tmp_file.write(payload)
                    tmp_file.flush()
                    if not skip_fsync:
                        os.fsync(tmp_file.fileno())
                os.replace(tmp_name, registry_path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def ensure_workspace(root: Path, config: LitehiveConfig | None = None) -> Path:
    root = _validate_workspace_root(
        root,
        source="ensure_workspace",
        allow_worktree_root_alias=False,
    )
    base = workspace_dir(root)
    tasks = base / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)

    cfg = config or LitehiveConfig()
    if not config_path(root).exists():
        initial_config = asdict(cfg) if config is not None else {}
        config_path(root).write_text(
            yaml.safe_dump(initial_config, sort_keys=False),
            encoding="utf-8",
        )

    if not context_path(root).exists():
        context_path(root).write_text(
            render_context_template(cfg.process_profile), encoding="utf-8"
        )

    if not workspace_gitignore_path(root).exists():
        workspace_gitignore_path(root).write_text(
            render_workspace_gitignore(),
            encoding="utf-8",
        )

    _register_workspace(root)

    # Import here to avoid circular import with litehive.storage
    from litehive.storage import runtime_store
    runtime_store(root).bootstrap()
    workspace_database_path(root).parent.mkdir(parents=True, exist_ok=True)

    return base
