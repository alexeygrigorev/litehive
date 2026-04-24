"""Workspace bootstrap helpers."""

import os
import re
from dataclasses import asdict
from pathlib import Path

import yaml

from litehive.config.model import LitehiveConfig
from litehive.config.paths import workspace_path
from litehive.config.workspace_files import config_path, context_path, workspace_dir, workspace_gitignore_path
from litehive.config.profiles.rendering import render_context_template
from litehive.config.registry import (
    list_registered_workspace_paths,
    register_workspace_path,
)

_UNRESOLVED_SHELL_VAR_RE = re.compile(r"(?<!\\)\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)")
_WORKSPACE_CONFIG_TEMPLATE = Path(__file__).resolve().parents[1] / "cli" / "templates" / "workspace_config.yaml"


def render_workspace_gitignore() -> str:
    return "\n".join(
        [
            ".lock",
            "pool-summary.txt",
            "engine-monitoring.yaml",
            "tasks/*/reports/commit_to_git-*.yaml",
            "",
        ]
    )


def registered_workspace_root(path: Path) -> Path | None:
    """Return the owning workspace root when ``path`` is inside a managed worktree."""
    resolved = path.resolve()
    if "worktrees" not in resolved.parts:
        return None
    for root in list_registered_workspace_paths():
        try:
            if resolved.is_relative_to(workspace_path(root, "worktrees").resolve()):
                return root.resolve()
        except OSError:
            continue
    return None


def _reject_invalid_workspace_path(path: Path | str, *, source: str) -> None:
    """Reject workspace paths containing unresolved shell variables."""
    raw = str(path).strip()
    match = _UNRESOLVED_SHELL_VAR_RE.search(raw)
    if match is not None:
        raise ValueError(
            f"invalid workspace root from {source}: {raw!r} contains unresolved shell variable "
            f"syntax ({match.group(0)!r}); pass the expanded absolute path instead"
        )


def _workspace_parent_root(path: Path) -> Path | None:
    for ancestor in path.parents:
        try:
            if workspace_dir(ancestor).is_dir():
                return ancestor
        except OSError:
            continue
    return None


def _task_matches(root: Path, task_id: str | None) -> bool:
    return task_id is None or _task_exists(root, task_id)


def _reject_litehive_control_paths(path: Path, *, source: str) -> None:
    """Reject workspace paths inside .litehive control directories or managed worktrees."""
    resolved_path = path.resolve()

    # Check if path is inside managed worktrees
    managed_worktree = next(
        (ancestor for ancestor in (resolved_path, *resolved_path.parents)
         if ancestor.name == "worktrees" and ancestor.parent.name == ".litehive"),
        None,
    )
    if managed_worktree is not None:
        raise ValueError(
            f"invalid workspace root from {source}: {resolved_path} is inside Litehive managed "
            f"worktrees at {managed_worktree}; choose the real repo root instead"
        )

    # Check if path is inside any .litehive control directory
    control_ancestor = next(
        (ancestor for ancestor in (resolved_path, *resolved_path.parents)
         if ancestor.name == ".litehive"),
        None,
    )
    if control_ancestor is not None:
        raise ValueError(
            f"invalid workspace root from {source}: {resolved_path} is inside the Litehive "
            f"control directory {control_ancestor}; choose the real repo root instead"
        )


def normalize_workspace_root(root: Path, *, source: str) -> Path:
    _reject_invalid_workspace_path(root, source=source)
    resolved_input = Path(root).expanduser().resolve()
    _reject_litehive_control_paths(resolved_input, source=source)

    resolved_root = registered_workspace_root(resolved_input) or resolved_input

    # Additional check for nested .litehive trees in resolved root
    if any(ancestor.name == ".litehive" for ancestor in resolved_root.parents):
        raise ValueError(
            f"invalid workspace root from {source}: {resolved_root} is nested inside another .litehive tree"
        )
    return resolved_root


def _reject_nested_workspace_bootstrap(root: Path, *, source: str) -> None:
    """Reject workspace creation inside existing Litehive workspace directories."""
    parent_workspace = _workspace_parent_root(root)
    if parent_workspace is None:
        return
    raise ValueError(
        f"invalid workspace root from {source}: {root} is inside existing Litehive workspace "
        f"{parent_workspace}; choose the real repo root instead of a nested subdirectory"
    )


def _task_exists(root: Path, task_id: str) -> bool:
    tasks_root = workspace_dir(root) / "tasks"
    return any(tasks_root.glob(f"{task_id}-*"))


def resolve_workspace(
    task_id: str | None,
    *,
    cwd: Path | None = None,
    register: bool = True,
) -> Path:
    effective_task_id = task_id or os.environ.get("LITEHIVE_TASK_ID")

    env_workspace = os.environ.get("LITEHIVE_WORKSPACE_ROOT")
    if env_workspace:
        resolved_env_workspace = normalize_workspace_root(Path(env_workspace), source="LITEHIVE_WORKSPACE_ROOT")
        if _task_matches(resolved_env_workspace, effective_task_id):
            if register:
                _register_workspace(resolved_env_workspace)
            return resolved_env_workspace

    search_root = (cwd or Path.cwd()).resolve()
    resolved_search_root = normalize_workspace_root(search_root, source=f"cwd:{search_root}")
    if resolved_search_root != search_root and _task_matches(resolved_search_root, effective_task_id):
        if register:
            _register_workspace(resolved_search_root)
        return resolved_search_root

    for candidate in (search_root, *search_root.parents):
        if not workspace_dir(candidate).is_dir():
            continue
        resolved = candidate.resolve()
        if not _task_matches(resolved, effective_task_id):
            continue
        if register:
            _register_workspace(resolved)
        return resolved

    if effective_task_id:
        for root in list_registered_workspace_paths():
            if _task_exists(root, effective_task_id):
                if register:
                    _register_workspace(root)
                return root

    raise ValueError(
        "unable to resolve workspace: set LITEHIVE_WORKSPACE_ROOT, run inside a Litehive workspace, "
        "or provide/set LITEHIVE_TASK_ID so the workspace registry can be used"
    )


def _register_workspace(root: Path) -> None:
    register_workspace_path(root.resolve())


def ensure_workspace(root: Path, config: LitehiveConfig | None = None) -> Path:
    root = normalize_workspace_root(root, source="ensure_workspace")
    _reject_nested_workspace_bootstrap(root, source="ensure_workspace")
    base = workspace_dir(root)
    tasks = base / "tasks"
    attention = base / "attention"
    tasks.mkdir(parents=True, exist_ok=True)
    attention.mkdir(parents=True, exist_ok=True)

    cfg = config or LitehiveConfig()
    if not config_path(root).exists():
        if config is None:
            config_path(root).write_text(
                _WORKSPACE_CONFIG_TEMPLATE.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        else:
            config_path(root).write_text(
                yaml.safe_dump(asdict(cfg), sort_keys=False),
                encoding="utf-8",
            )

    if not context_path(root).exists():
        context_path(root).write_text(render_context_template(cfg.process_profile), encoding="utf-8")

    if not workspace_gitignore_path(root).exists():
        workspace_gitignore_path(root).write_text(
            render_workspace_gitignore(),
            encoding="utf-8",
        )

    _register_workspace(root)

    workspace_path(root, "data.db").parent.mkdir(parents=True, exist_ok=True)
    from litehive.db.schema import connect_workspace_db
    from litehive.state.store import RuntimeStore

    with connect_workspace_db(root) as connection:
        RuntimeStore._ensure_workspace_state_rows(connection)

    return base
