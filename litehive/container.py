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
    workspace = Workspace.from_path(root)
    return LitehiveContainer(
        workspace=workspace,
        config=workspace.config(),
    )
