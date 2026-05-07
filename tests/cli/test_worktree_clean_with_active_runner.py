"""Tests for worktree clean behavior when runner is active."""

from pathlib import Path
from unittest.mock import Mock

from litehive.cli.worktree_cli import clean


def test_worktree_clean_defers_metadata_clear_when_runner_active(tmp_path: Path, monkeypatch):
    """Test that worktree clean gracefully handles WorkspaceConflictError by deferring metadata clearing."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)

    # Mock the workspace to be valid
    monkeypatch.setattr("litehive.config.workspace.create_workspace", lambda x: None)

    # Mock collect_managed_worktrees to return a cleanable worktree
    mock_worktree = Mock()
    mock_worktree.task_id = "T-0001-test-task"
    mock_worktree.status = "completed"
    mock_worktree.change_count = 0
    mock_worktree.worktree_rel = "worktrees/T-0001-test-task"
    mock_worktree.worktree_path = tmp_path / "worktree"
    mock_worktree.cleanable = True
    mock_worktree.active = False

    # Mock the unified worktree removal function to return deferred results
    def mock_remove_cleanable_worktrees(workspace, *, dry_run=False):
        if dry_run:
            return {
                "candidates": [mock_worktree],
                "skipped_active": [],
                "removed": [],
                "deferred": [],
                "failures": [],
            }
        else:
            # Simulate a deferred metadata clear (what happens when WorkspaceConflictError is raised)
            return {
                "candidates": [mock_worktree],
                "skipped_active": [],
                "removed": [],
                "deferred": [mock_worktree],  # Deferred due to conflict
                "failures": [],
            }

    monkeypatch.setattr(
        "litehive.worktree.service.WorktreeService.remove_cleanable_worktrees",
        lambda self, *, dry_run=False: mock_remove_cleanable_worktrees(tmp_path, dry_run=dry_run),
    )

    # Capture stdout
    import io
    import sys

    captured_output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured_output)

    # Run the clean command
    result = clean(workspace=workspace, dry_run=False)

    # Verify the command completed successfully (didn't crash)
    assert result == 0

    # Verify output shows deferred operation
    output = captured_output.getvalue()
    assert "deferred_metadata_clear: T-0001-test-task" in output
    assert "deferred_count: 1" in output


def test_worktree_clean_succeeds_when_no_runner_conflict(tmp_path: Path, monkeypatch):
    """Test that worktree clean works normally when there's no runner conflict."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)

    # Mock the workspace to be valid
    monkeypatch.setattr("litehive.config.workspace.create_workspace", lambda x: None)

    # Mock collect_managed_worktrees to return a cleanable worktree
    mock_worktree = Mock()
    mock_worktree.task_id = "T-0002-test-task"
    mock_worktree.status = "completed"
    mock_worktree.change_count = 0
    mock_worktree.worktree_rel = "worktrees/T-0002-test-task"
    mock_worktree.worktree_path = tmp_path / "worktree"
    mock_worktree.cleanable = True
    mock_worktree.active = False

    # Mock the unified worktree removal function to return successful results
    def mock_remove_cleanable_worktrees(workspace, *, dry_run=False):
        if dry_run:
            return {
                "candidates": [mock_worktree],
                "skipped_active": [],
                "removed": [],
                "deferred": [],
                "failures": [],
            }
        else:
            # Simulate successful removal
            return {
                "candidates": [mock_worktree],
                "skipped_active": [],
                "removed": [mock_worktree],
                "deferred": [],
                "failures": [],
            }

    monkeypatch.setattr(
        "litehive.worktree.service.WorktreeService.remove_cleanable_worktrees",
        lambda self, *, dry_run=False: mock_remove_cleanable_worktrees(tmp_path, dry_run=dry_run),
    )

    # Capture stdout
    import io
    import sys

    captured_output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured_output)

    # Run the clean command
    result = clean(workspace=workspace, dry_run=False)

    # Verify the command completed successfully
    assert result == 0

    # Verify output shows successful removal
    output = captured_output.getvalue()
    assert "removed: T-0002-test-task" in output
    assert "removed_count: 1" in output
    assert "deferred_count: 0" in output
