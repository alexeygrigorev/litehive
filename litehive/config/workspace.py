"""
Workspace bootstrap and resolution helpers.

Owns three responsibilities: validating and normalizing a candidate
workspace path (rejecting Litehive-internal paths), resolving the right
workspace for a CLI invocation when no explicit path is given, and
bootstrapping a workspace on first use (directories, seeded config,
runtime store).
"""

import os
from dataclasses import asdict
from pathlib import Path

import yaml

from litehive.config.model import LitehiveConfig
from litehive.config.paths import workspace_path
from litehive.config.workspace_files import config_path, context_path, workspace_dir, workspace_gitignore_path
from litehive.config.profiles.rendering import render_context_template


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


def _workspace_config_template_path() -> Path:
    """
    Return the built-in workspace config template path.
    """
    return Path(__file__).resolve().parents[1] / "cli" / "templates" / "workspace_config.yaml"


def require_existing_workspace(root: Path, source: str) -> Path:
    """
    Return ``root`` when it is already a Litehive workspace.

    Loading paths call this instead of ``ensure_workspace`` so a read
    operation cannot silently bootstrap a new project. Workspace
    creation remains explicit through ``ensure_workspace``.
    """
    resolved_root = normalize_workspace_root(root, source=source)
    if workspace_dir(resolved_root).is_dir():
        return resolved_root
    raise ValueError(
        f"unable to load workspace from {source}: {resolved_root} is not an existing Litehive project; "
        "run `litehive init` first"
    )


def _reject_litehive_control_paths(path: Path, source: str) -> None:
    """
    Reject paths inside ``.litehive`` control dirs.

    Bootstrapping a workspace there would create a nested
    ``.litehive`` inside another one and produce subtle
    cross-talk; refusing up front keeps the failure mode obvious
    instead of letting the operator discover the corruption
    later.
    """
    resolved_path = path.resolve()

    for ancestor in (resolved_path, *resolved_path.parents):
        if ancestor.name != ".litehive":
            continue
        raise ValueError(
            f"invalid workspace root from {source}: {resolved_path} is inside the Litehive "
            f"control directory {ancestor}; choose the real repo root instead"
        )


def normalize_workspace_root(root: Path, source: str) -> Path:
    """
    Resolve a candidate workspace path.

    Callers that read a workspace must check that ``<root>/.litehive``
    exists; callers that create a workspace must reject Litehive
    control paths before bootstrapping.
    """
    return Path(root).expanduser().resolve()


def _task_exists_in_state(root: Path, task_id: str) -> bool:
    """
    Return whether SQLite task state owns ``task_id`` in ``root``.
    """
    # inline: state.records imports workspace helpers, so keep this local
    # to avoid a config.workspace -> state.records import cycle.
    from litehive.state.records import task_exists  # noqa: PLC0415

    return task_exists(root, task_id)


def resolve_workspace(
    task_id: str | None,
    cwd: Path | None = None,
) -> Path:
    """
    Pick the right workspace for a CLI invocation.

    Precedence: explicit ``cwd``, ``LITEHIVE_WORKSPACE_ROOT`` env,
    then the actual current cwd. The selected directory must already
    be a Litehive workspace; resolution no longer walks parent
    directories or searches globally by task id.
    """
    effective_task_id = task_id
    if effective_task_id is None and cwd is None:
        effective_task_id = os.environ.get("LITEHIVE_TASK_ID")
    env_workspace = os.environ.get("LITEHIVE_WORKSPACE_ROOT")
    if cwd is None and env_workspace:
        workspace_root = require_existing_workspace(Path(env_workspace), source="LITEHIVE_WORKSPACE_ROOT")
    else:
        workspace_root = require_existing_workspace(cwd or Path.cwd(), source="cwd")

    if effective_task_id is not None and not _task_exists_in_state(workspace_root, effective_task_id):
        raise ValueError(f"unable to resolve workspace: task {effective_task_id} is not in {workspace_root}")
    return workspace_root


def ensure_workspace(
    root: Path,
    config: LitehiveConfig | None = None,
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
    root = normalize_workspace_root(root, source="ensure_workspace")
    _reject_litehive_control_paths(root, source="ensure_workspace")
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

    workspace_path(root, "data.db").parent.mkdir(parents=True, exist_ok=True)
    # inline: state.store transitively pulls db.schema which loads config.*
    # back through litehive/config/__init__.py during partial init.
    from litehive.state.store import runtime_store  # noqa: PLC0415

    runtime_store(root).bootstrap()

    return base
