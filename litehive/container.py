"""Dependency container for workspace-scoped Litehive services."""

from dataclasses import dataclass
from pathlib import Path

from litehive.config.model import LitehiveConfig
from litehive.workspace import Workspace


@dataclass(frozen=True)
class LitehiveContainer:
    """
    Ready dependency graph for one workspace.

    Constructed by :func:`build_container` at process boundaries. The
    dataclass constructor only stores dependencies, so tests can build
    containers with fakes and production wiring stays in one module.
    """

    workspace: Workspace
    config: LitehiveConfig


def build_container(root: Path) -> LitehiveContainer:
    """
    Convert a raw workspace path into the process-level dependency graph.

    This is the production assembly point for workspace-scoped
    dependencies. Internal helpers should receive the resulting
    container, its ``workspace``, or a focused service from it rather
    than rebuilding dependencies from ``root``.
    """
    workspace = build_workspace(root)
    return LitehiveContainer(
        workspace=workspace,
        config=workspace.config(),
    )


def build_workspace(root: Path) -> Workspace:
    """
    Convert a raw workspace path into the workspace dependency only.

    Use this for read-only paths that must not load config or bootstrap
    persistence as a side effect. Full process wiring should prefer
    :func:`build_container`.
    """
    return Workspace.from_path(root)


def build_subagent_manager(root: Path, execution_root: Path, manager_cls=None):
    """
    Assemble a ``SubagentManager`` for one agent turn.

    Kept in the container so ``SubagentManager.__init__`` only receives
    ready collaborators and never builds workspace/config/sandbox itself.
    """
    from litehive.agents.manager import SubagentManager  # noqa: PLC0415
    from litehive.agents.sandbox import SandboxLauncher  # noqa: PLC0415

    if manager_cls is not None and manager_cls is not SubagentManager:
        return manager_cls(root, execution_root=execution_root)

    container = build_container(root)
    sandbox = SandboxLauncher(container.workspace.root, container.config)
    manager_type = manager_cls or SubagentManager
    return manager_type(
        container.workspace.root,
        execution_root=execution_root,
        workspace=container.workspace,
        config=container.config,
        sandbox=sandbox,
    )
