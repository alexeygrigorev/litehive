"""Repo-local `.litehive` file paths."""

from pathlib import Path


def workspace_dir(root: Path) -> Path:
    """Return the ``<root>/.litehive`` directory; the single canonical answer to "where does Litehive store this workspace's metadata?"."""
    return root / ".litehive"


def config_path(root: Path) -> Path:
    """Path to the workspace YAML config; consumed by ``LitehiveConfig`` loader and updated by ``ensure_workspace`` on first use."""
    return workspace_dir(root) / "config.yaml"


def context_path(root: Path) -> Path:
    """Path to the workspace's process-context Markdown rendered from the active process profile; included in agent prompts as the project description block."""
    return workspace_dir(root) / "context.md"


def workspace_gitignore_path(root: Path) -> Path:
    """Path to the ``.gitignore`` written into ``.litehive/`` so workspace runtime artifacts (locks, summaries) never leak into the user's commits."""
    return workspace_dir(root) / ".gitignore"
