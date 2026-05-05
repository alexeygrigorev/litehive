"""
Repo-local ``.litehive`` file paths.

Centralizes the names of files Litehive writes inside a checked-out
repository (config, context document, gitignore) so every caller
agrees on the layout and renames stay tractable. The
``workspace_data_dir`` paths under :mod:`litehive.config.paths`
are the *runtime* counterpart; this module is for in-repo
artifacts.
"""

from pathlib import Path


def workspace_dir(root: Path) -> Path:
    """
    Return the ``<root>/.litehive`` directory.

    The single canonical answer to "where does Litehive store
    this workspace's in-repo metadata?". Pure path math — does
    not create the directory; ``ensure_workspace`` is the
    bootstrap path.
    """
    return root / ".litehive"


def config_path(root: Path) -> Path:
    """
    Path to the workspace YAML config.

    Consumed by the :class:`LitehiveConfig` loader and seeded by
    ``ensure_workspace`` on first use. Lives in the repo so the
    config travels with the source rather than being scoped to a
    particular checkout.
    """
    return workspace_dir(root) / "config.yaml"


def context_path(root: Path) -> Path:
    """
    Path to the workspace's process-context Markdown.

    Rendered from the active process profile and included in
    agent prompts as the project description block; the file
    lives in-repo so changes show up in normal review and travel
    with the codebase.
    """
    return workspace_dir(root) / "context.md"


def workspace_gitignore_path(root: Path) -> Path:
    """
    Path to the ``.gitignore`` written into ``.litehive/``.

    Keeps workspace runtime artifacts (locks, summaries, transient
    logs) out of the user's commits. Generated rather than
    committed by hand so the ignore list always matches the files
    Litehive actually writes.
    """
    return workspace_dir(root) / ".gitignore"
