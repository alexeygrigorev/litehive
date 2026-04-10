"""Workspace bootstrap helpers."""

import os
from dataclasses import asdict
from pathlib import Path

import yaml

from litehive.config.model import LitehiveConfig
from litehive.config.paths import (
    config_path,
    context_path,
    global_config_path,
    state_path,
    workspace_dir,
    workspace_gitignore_path,
)
from litehive.config.profiles import render_context_template


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


def _workspace_registry_paths() -> list[Path]:
    return [
        Path.home() / ".litehive" / "workspaces.yaml",
        global_config_path().parent / "workspaces.yaml",
    ]


def _iter_registry_workspace_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for registry_path in _workspace_registry_paths():
        if not registry_path.exists():
            continue
        data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
        workspaces = data.get("workspaces", data) if isinstance(data, dict) else {}
        if not isinstance(workspaces, dict):
            continue
        for payload in workspaces.values():
            candidate: str | None = None
            if isinstance(payload, str):
                candidate = payload
            elif isinstance(payload, dict):
                for key in ("path", "root", "workspace", "repo_path"):
                    value = payload.get(key)
                    if isinstance(value, str):
                        candidate = value
                        break
            if not candidate:
                continue
            try:
                resolved = _validate_workspace_root(Path(candidate), source=str(registry_path))
            except ValueError:
                continue
            if resolved in seen:
                continue
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
        return _validate_workspace_root(workspace, source="--workspace")

    env_workspace = os.environ.get("LITEHIVE_WORKSPACE_ROOT")
    if env_workspace:
        resolved_env_workspace = _validate_workspace_root(
            Path(env_workspace), source="LITEHIVE_WORKSPACE_ROOT"
        )
        if not effective_task_id or _task_exists(resolved_env_workspace, effective_task_id):
            return resolved_env_workspace

    search_root = (cwd or Path.cwd()).resolve()
    for candidate in (search_root, *search_root.parents):
        if not workspace_dir(candidate).is_dir():
            continue
        resolved = _validate_workspace_root(candidate, source=f"cwd:{search_root}")
        if effective_task_id and not _task_exists(resolved, effective_task_id):
            continue
        return resolved

    if effective_task_id:
        for root in _iter_registry_workspace_roots():
            if _task_exists(root, effective_task_id):
                return root

    raise ValueError(
        "unable to resolve workspace: provide --workspace, set LITEHIVE_WORKSPACE_ROOT, "
        "run inside a Litehive workspace, or set LITEHIVE_TASK_ID so the workspace registry can be used"
    )


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

    if not state_path(root).exists():
        state_path(root).write_text(
            yaml.safe_dump(
                {
                    "active_task_id": None,
                    "mode": cfg.implementation_mode_name,
                    "queue": [],
                    "pool_stop_reason": None,
                    "next_task_number": 0,
                },
                sort_keys=False,
            ),
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

    return base
