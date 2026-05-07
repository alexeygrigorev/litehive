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


def build_subagent_manager_for_workspace(
    workspace: Workspace,
    config: LitehiveConfig,
    execution_root: Path,
    manager_cls=None,
):
    """
    Assemble a ``SubagentManager`` from injected workspace dependencies.

    Kept in the container so ``SubagentManager.__init__`` only receives
    ready collaborators and never builds workspace/config/sandbox itself.
    """
    from litehive.agents.manager import SubagentManager  # noqa: PLC0415
    from litehive.sandbox.launcher import DockerSandboxLauncher  # noqa: PLC0415
    from litehive.agents.engine_manager import EngineManager  # noqa: PLC0415
    from litehive.agents.session import SubagentSessionManager  # noqa: PLC0415
    from litehive.agents.session_inactivity import (  # noqa: PLC0415
        SubagentInactivityMonitor,
        SubagentInactivityTimeoutPolicy,
    )
    from litehive.agents.session_streams import SubagentStreamLog  # noqa: PLC0415
    from litehive.agents.subagent_ids import SubagentIdRepository  # noqa: PLC0415

    if manager_cls is not None and manager_cls is not SubagentManager:
        return manager_cls(workspace.root, execution_root=execution_root)

    sandbox = DockerSandboxLauncher(workspace.root, config)
    engines = EngineManager()
    sessions = SubagentSessionManager(
        root=workspace.root,
        workspace=workspace,
        sandbox=sandbox,
        config=config,
        inactivity_monitor=SubagentInactivityMonitor(SubagentInactivityTimeoutPolicy(config)),
        stream_log=SubagentStreamLog(),
    )
    subagent_ids = SubagentIdRepository(workspace)
    manager_type = manager_cls or SubagentManager
    return manager_type(
        workspace.root,
        execution_root=execution_root,
        workspace=workspace,
        config=config,
        sandbox=sandbox,
        sessions=sessions,
        engines=engines,
        subagent_ids=subagent_ids,
    )


def build_subagent_manager(root: Path, execution_root: Path, manager_cls=None):
    """
    Assemble a ``SubagentManager`` for one agent turn from a raw path.

    Process boundaries and tests can use this wrapper; internal callers
    that already have workspace dependencies should call
    :func:`build_subagent_manager_for_workspace`.
    """
    container = build_container(root)
    return build_subagent_manager_for_workspace(
        container.workspace,
        container.config,
        execution_root=execution_root,
        manager_cls=manager_cls,
    )
