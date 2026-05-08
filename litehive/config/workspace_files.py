"""
Repo-local ``.litehive`` file paths.

Centralizes the names of files Litehive writes inside a checked-out
repository (config, context document, gitignore) so every caller
agrees on the layout and renames stay tractable. The
``workspace_data_dir`` paths under :mod:`litehive.config.paths`
are the *runtime* counterpart; this module is for in-repo
artifacts.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceControlFiles:
    """
    Bound paths for one workspace's repo-local ``.litehive`` files.

    Most production code should get this from ``Workspace.control_files()``
    so the validated workspace root stays attached to the path helpers
    instead of being threaded through separate free functions.
    """

    root: Path

    def directory(self) -> Path:
        """
        Return the ``<root>/.litehive`` directory.
        """
        return self.root / ".litehive"

    def config(self) -> Path:
        """
        Return the workspace YAML config path.
        """
        return self.directory() / "config.yaml"

    def context(self) -> Path:
        """
        Return the workspace's process-context Markdown path.
        """
        return self.directory() / "context.md"

    def gitignore(self) -> Path:
        """
        Return the ``.gitignore`` path written into ``.litehive/``.
        """
        return self.directory() / ".gitignore"


def workspace_dir(root: Path) -> Path:
    """
    Return the ``<root>/.litehive`` directory.

    The single canonical answer to "where does Litehive store
    this workspace's in-repo metadata?". Pure path math — does
    not create the directory; ``create_workspace`` is the
    bootstrap path.
    """
    return WorkspaceControlFiles(root).directory()


def config_path(root: Path) -> Path:
    """
    Path to the workspace YAML config.

    Consumed by the :class:`LitehiveConfig` loader and seeded by
    ``create_workspace`` on first use. Lives in the repo so the
    config travels with the source rather than being scoped to a
    particular checkout.
    """
    return WorkspaceControlFiles(root).config()


def context_path(root: Path) -> Path:
    """
    Path to the workspace's process-context Markdown.

    Rendered from the active process profile and included in
    agent prompts as the project description block; the file
    lives in-repo so changes show up in normal review and travel
    with the codebase.
    """
    return WorkspaceControlFiles(root).context()


def workspace_gitignore_path(root: Path) -> Path:
    """
    Path to the ``.gitignore`` written into ``.litehive/``.

    Keeps workspace runtime artifacts (locks, summaries, transient
    logs) out of the user's commits. Generated rather than
    committed by hand so the ignore list always matches the files
    Litehive actually writes.
    """
    return WorkspaceControlFiles(root).gitignore()
