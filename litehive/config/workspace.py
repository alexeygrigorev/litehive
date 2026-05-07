"""
Workspace bootstrap and resolution helpers.

Owns three responsibilities: validating and normalizing a candidate
workspace path (rejecting unresolved shell vars and Litehive-internal
paths), resolving the right workspace for a CLI invocation when no
explicit path is given, and bootstrapping a workspace on first use
(directories, seeded config, runtime store).
"""

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
    WorkspaceRegistry,
    build_workspace_registry,
)


def render_workspace_gitignore() -> str:
    """
    Produce the ``.gitignore`` body written into ``.litehive/``.

    Lists the runtime artifacts that must never end up in the
    user's commits (lockfile, pool summary). Generated rather
    than checked-in by hand so the ignore list stays in sync
    with the files Litehive actually writes.
    """
    return "\n".join(
        [
            ".lock",
            "pool-summary.txt",
            "",
        ]
    )


def registered_workspace_root(path: Path, registry: WorkspaceRegistry | None = None) -> Path | None:
    """
    Return the owning workspace root when ``path`` is inside a managed worktree.

    Used by :func:`normalize_workspace_root` so a CLI run from
    inside a worktree directory still resolves to the real
    workspace and not the worktree path. Returns ``None`` when
    the path is not under any registered workspace's worktrees
    directory.
    """
    resolved = path.resolve()
    if "worktrees" not in resolved.parts:
        return None
    workspace_registry = registry or build_workspace_registry()
    for root in workspace_registry.list_paths():
        try:
            if resolved.is_relative_to(workspace_path(root, "worktrees").resolve()):
                return root.resolve()
        except OSError:
            continue
    return None


def _reject_invalid_workspace_path(path: Path | str, source: str) -> None:
    """
    Reject workspace paths containing unresolved shell variables.

    Catches the classic mistake of passing ``$WORKSPACE`` literally
    (single-quoted, missed expansion) instead of the expanded
    absolute path; without this the workspace would be bootstrapped
    inside a directory literally named ``$WORKSPACE`` and the
    operator would chase ghosts. ``source`` tells the operator
    which CLI surface produced the bad value.
    """
    raw = str(path).strip()
    shell_variable_pattern = r"(?<!\\)\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)"
    match = re.search(shell_variable_pattern, raw)
    if match is not None:
        raise ValueError(
            f"invalid workspace root from {source}: {raw!r} contains unresolved shell variable "
            f"syntax ({match.group(0)!r}); pass the expanded absolute path instead"
        )


def _workspace_config_template_path() -> Path:
    """
    Return the built-in workspace config template path.
    """
    return Path(__file__).resolve().parents[1] / "cli" / "templates" / "workspace_config.yaml"


def _workspace_parent_root(path: Path) -> Path | None:
    """
    Walk up the parents looking for an existing ``.litehive`` directory.

    Used to detect attempts to bootstrap a workspace inside
    another one — a nested workspace would silently capture
    operator commands meant for the outer workspace, so the
    bootstrap path refuses it loudly.
    """
    for ancestor in path.parents:
        try:
            if workspace_dir(ancestor).is_dir():
                return ancestor
        except OSError:
            continue
    return None


def _task_matches(root: Path, task_id: str | None) -> bool:
    """
    Workspace-resolution predicate.

    A candidate root is acceptable when no task id constrains the
    search or when the workspace actually owns that task. Lets
    :func:`resolve_workspace` skip past unrelated workspaces in
    the registry when a task id was provided.
    """
    return task_id is None or _task_exists(root, task_id)


def _reject_litehive_control_paths(path: Path, source: str) -> None:
    """
    Reject paths inside ``.litehive`` control dirs or managed worktrees.

    Bootstrapping a workspace there would create a nested
    ``.litehive`` inside another one and produce subtle
    cross-talk; refusing up front keeps the failure mode obvious
    instead of letting the operator discover the corruption
    later.
    """
    resolved_path = path.resolve()

    # Check if path is inside managed worktrees
    managed_worktree = next(
        (
            ancestor
            for ancestor in (resolved_path, *resolved_path.parents)
            if ancestor.name == "worktrees" and ancestor.parent.name == ".litehive"
        ),
        None,
    )
    if managed_worktree is not None:
        raise ValueError(
            f"invalid workspace root from {source}: {resolved_path} is inside Litehive managed "
            f"worktrees at {managed_worktree}; choose the real repo root instead"
        )

    # Check if path is inside any .litehive control directory
    control_ancestor = next(
        (ancestor for ancestor in (resolved_path, *resolved_path.parents) if ancestor.name == ".litehive"),
        None,
    )
    if control_ancestor is not None:
        raise ValueError(
            f"invalid workspace root from {source}: {resolved_path} is inside the Litehive "
            f"control directory {control_ancestor}; choose the real repo root instead"
        )


def normalize_workspace_root(root: Path, source: str, registry: WorkspaceRegistry | None = None) -> Path:
    """
    Validate, expand, and re-route a candidate workspace path.

    Rejects unresolved shell vars and Litehive-internal paths,
    and redirects worktree paths back to the owning workspace so
    a CLI run inside a managed worktree still acts on the real
    workspace. ``source`` is woven into errors so an operator
    knows which input (env, ``--workspace``, …) produced a bad
    value.
    """
    _reject_invalid_workspace_path(root, source=source)
    resolved_input = Path(root).expanduser().resolve()
    _reject_litehive_control_paths(resolved_input, source=source)

    resolved_root = registered_workspace_root(resolved_input, registry=registry) or resolved_input

    # Additional check for nested .litehive trees in resolved root
    if any(ancestor.name == ".litehive" for ancestor in resolved_root.parents):
        raise ValueError(
            f"invalid workspace root from {source}: {resolved_root} is nested inside another .litehive tree"
        )
    return resolved_root


def _reject_nested_workspace_bootstrap(root: Path, source: str) -> None:
    """
    Reject workspace creation inside an existing Litehive workspace.

    Distinct from :func:`_reject_litehive_control_paths` because
    the existing-workspace check looks at parent ``.litehive``
    directories — a nested workspace under an unrelated parent
    workspace would silently break command routing and is rarely
    what the operator intended.
    """
    parent_workspace = _workspace_parent_root(root)
    if parent_workspace is None:
        return
    raise ValueError(
        f"invalid workspace root from {source}: {root} is inside existing Litehive workspace "
        f"{parent_workspace}; choose the real repo root instead of a nested subdirectory"
    )


def _task_exists(root: Path, task_id: str) -> bool:
    """
    Cheap on-disk check for "does this workspace own this task?".

    Looks for a directory under ``.litehive/tasks/`` whose name
    starts with ``task_id-``. Used by workspace resolution to
    disambiguate when a task id is supplied — far cheaper than
    loading the full task store and good enough because task ids
    are unique within a workspace.
    """
    tasks_root = workspace_dir(root) / "tasks"
    return any(tasks_root.glob(f"{task_id}-*"))


def _resolve_workspace_from_search_root(
    search_root: Path,
    effective_task_id: str | None,
    register: bool,
    registry: WorkspaceRegistry,
) -> Path | None:
    """
    Resolve a workspace by walking ancestors of ``search_root``.

    Called by :func:`resolve_workspace` for both the cwd-driven
    and explicit-cwd lookup phases — same logic, run at two
    different points in the resolution chain. Returns ``None``
    when no ancestor directory carries a ``.litehive`` directory
    that matches the optional task constraint.
    """
    resolved_search_root = normalize_workspace_root(search_root, source=f"cwd:{search_root}", registry=registry)
    if resolved_search_root != search_root and _task_matches(resolved_search_root, effective_task_id):
        if register:
            _register_workspace(resolved_search_root, registry)
        return resolved_search_root

    for candidate in (search_root, *search_root.parents):
        if not workspace_dir(candidate).is_dir():
            continue
        resolved = candidate.resolve()
        if not _task_matches(resolved, effective_task_id):
            continue
        if register:
            _register_workspace(resolved, registry)
        return resolved
    return None


def resolve_workspace(
    task_id: str | None,
    cwd: Path | None = None,
    register: bool = True,
    registry: WorkspaceRegistry | None = None,
) -> Path:
    """
    Pick the right workspace for a CLI invocation.

    Precedence: explicit ``cwd``, ``LITEHIVE_WORKSPACE_ROOT``
    env, the actual current cwd, and finally the registry by task
    id. The fallback chain is the contract every CLI command
    relies on for ``--workspace``-less invocation; reordering it
    would break invocation patterns that hooks and operator
    scripts rely on.
    """
    effective_task_id = task_id
    if effective_task_id is None and cwd is None:
        effective_task_id = os.environ.get("LITEHIVE_TASK_ID")
    workspace_registry = registry or build_workspace_registry()
    search_root = (cwd or Path.cwd()).resolve()

    if cwd is not None:
        resolved = _resolve_workspace_from_search_root(
            search_root,
            effective_task_id=effective_task_id,
            register=register,
            registry=workspace_registry,
        )
        if resolved is not None:
            return resolved

    env_workspace = os.environ.get("LITEHIVE_WORKSPACE_ROOT")
    if env_workspace:
        resolved_env_workspace = normalize_workspace_root(
            Path(env_workspace),
            source="LITEHIVE_WORKSPACE_ROOT",
            registry=workspace_registry,
        )
        if _task_matches(resolved_env_workspace, effective_task_id):
            if register:
                _register_workspace(resolved_env_workspace, workspace_registry)
            return resolved_env_workspace

    resolved = _resolve_workspace_from_search_root(
        search_root,
        effective_task_id=effective_task_id,
        register=register,
        registry=workspace_registry,
    )
    if resolved is not None:
        return resolved

    if effective_task_id:
        for root in workspace_registry.list_paths():
            if _task_exists(root, effective_task_id):
                if register:
                    _register_workspace(root, workspace_registry)
                return root

    raise ValueError(
        "unable to resolve workspace: set LITEHIVE_WORKSPACE_ROOT, run inside a Litehive workspace, "
        "or provide/set LITEHIVE_TASK_ID so the workspace registry can be used"
    )


def _register_workspace(root: Path, registry: WorkspaceRegistry) -> None:
    """
    Persist the workspace path to the cross-process registry.

    Lets later CLI calls — especially those driven only by task
    id, e.g. inside hook scripts — find the workspace without an
    explicit ``--workspace`` flag. Always uses the resolved
    absolute path so the registry never carries relative entries.
    """
    registry.register_path(root.resolve())


def ensure_workspace(
    root: Path,
    config: LitehiveConfig | None = None,
    registry: WorkspaceRegistry | None = None,
) -> Path:
    """
    Bootstrap a workspace on first use.

    Creates directories, seeds config/context/gitignore, registers
    the path, and bootstraps the runtime store. Idempotent so
    every CLI command can call it at startup without first
    checking whether the workspace already exists; ``config`` is
    only consulted on first creation, established workspaces are
    not rewritten.
    """
    workspace_registry = registry or build_workspace_registry()
    root = normalize_workspace_root(root, source="ensure_workspace", registry=workspace_registry)
    _reject_nested_workspace_bootstrap(root, source="ensure_workspace")
    base = workspace_dir(root)
    tasks = base / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)

    cfg = config or LitehiveConfig()
    if not config_path(root).exists():
        if config is None:
            config_path(root).write_text(
                _workspace_config_template_path().read_text(encoding="utf-8"),
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

    _register_workspace(root, workspace_registry)

    workspace_path(root, "data.db").parent.mkdir(parents=True, exist_ok=True)
    # inline: state.store transitively pulls db.schema which loads config.*
    # back through litehive/config/__init__.py during partial init.
    from litehive.state.store import runtime_store  # noqa: PLC0415

    runtime_store(root).bootstrap()

    return base
