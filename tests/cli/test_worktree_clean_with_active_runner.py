"""Tests for worktree clean behavior when runner is active."""

from pathlib import Path
from unittest.mock import Mock
import pytest

from litehive.cli.worktree_cli import clean
from litehive.domain.task_ops import WorkspaceConflictError


def test_worktree_clean_defers_metadata_clear_when_runner_active(tmp_path: Path, monkeypatch):
    """Test that worktree clean gracefully handles WorkspaceConflictError by deferring metadata clearing."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)

    # Mock the workspace to be valid
    monkeypatch.setattr("litehive.config.workspace.ensure_workspace", lambda x: None)

    # Mock collect_managed_worktrees to return a cleanable worktree
    mock_worktree = Mock()
    mock_worktree.task_id = "T-0001-test-task"
    mock_worktree.status = "completed"
    mock_worktree.change_count = 0
    mock_worktree.worktree_rel = "worktrees/T-0001-test-task"
    mock_worktree.worktree_path = tmp_path / "worktree"
    mock_worktree.cleanable = True
    mock_worktree.active = False

    monkeypatch.setattr("litehive.cli.worktree_cli.collect_managed_worktrees", lambda x: [mock_worktree])

    # Mock remove_worktree to succeed
    monkeypatch.setattr("litehive.cli.worktree_cli.remove_worktree", lambda *args, **kwargs: None)

    # Create a mock task
    mock_task = Mock()
    mock_task.runtime.git.worktree_path = "worktrees/T-0001-test-task"

    # Mock get_task to return the task
    monkeypatch.setattr("litehive.cli.worktree_cli.get_task", lambda workspace, task_id: mock_task)

    # Mock save_task to raise WorkspaceConflictError (simulating active runner)
    def mock_save_task(workspace, task):
        raise WorkspaceConflictError("workspace is already being mutated by another runner")

    monkeypatch.setattr("litehive.cli.worktree_cli.save_task", mock_save_task)

    # Mock clear_task_worktree_path to do nothing
    monkeypatch.setattr("litehive.cli.worktree_cli.clear_task_worktree_path", lambda task: None)

    # Mock attention system
    attention_items = []
    def mock_record_attention(workspace, **kwargs):
        attention_items.append(kwargs)
        return Mock(id=1)

    monkeypatch.setattr("litehive.cli.worktree_cli.record_attention", mock_record_attention)

    # Capture stdout
    import io
    import sys
    captured_output = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', captured_output)

    # Run the clean command
    result = clean(workspace=workspace, dry_run=False)

    # Verify the command completed successfully (didn't crash)
    assert result == 0

    # Verify that an attention item was recorded for deferred metadata clearing
    assert len(attention_items) == 1
    attention = attention_items[0]
    assert attention["kind"] == "stale_worktree_metadata"
    assert attention["task_id"] == "T-0001-test-task"
    assert "deferred" in attention["title"].lower()
    assert "active runner lock" in attention["reason"]

    # Verify output shows deferred operation
    output = captured_output.getvalue()
    assert "deferred_metadata_clear: T-0001-test-task" in output
    assert "deferred_count: 1" in output


def test_worktree_clean_succeeds_when_no_runner_conflict(tmp_path: Path, monkeypatch):
    """Test that worktree clean works normally when there's no runner conflict."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)

    # Mock the workspace to be valid
    monkeypatch.setattr("litehive.config.workspace.ensure_workspace", lambda x: None)

    # Mock collect_managed_worktrees to return a cleanable worktree
    mock_worktree = Mock()
    mock_worktree.task_id = "T-0002-test-task"
    mock_worktree.status = "completed"
    mock_worktree.change_count = 0
    mock_worktree.worktree_rel = "worktrees/T-0002-test-task"
    mock_worktree.worktree_path = tmp_path / "worktree"
    mock_worktree.cleanable = True
    mock_worktree.active = False

    monkeypatch.setattr("litehive.cli.worktree_cli.collect_managed_worktrees", lambda x: [mock_worktree])

    # Mock remove_worktree to succeed
    monkeypatch.setattr("litehive.cli.worktree_cli.remove_worktree", lambda *args, **kwargs: None)

    # Create a mock task
    mock_task = Mock()
    mock_task.runtime.git.worktree_path = "worktrees/T-0002-test-task"

    # Mock get_task to return the task
    monkeypatch.setattr("litehive.cli.worktree_cli.get_task", lambda workspace, task_id: mock_task)

    # Mock save_task to succeed (no runner conflict)
    monkeypatch.setattr("litehive.cli.worktree_cli.save_task", lambda workspace, task: None)

    # Mock clear_task_worktree_path to do nothing
    monkeypatch.setattr("litehive.cli.worktree_cli.clear_task_worktree_path", lambda task: None)

    # Mock attention system (should not be called)
    def mock_record_attention(workspace, **kwargs):
        pytest.fail("record_attention should not be called when save_task succeeds")

    monkeypatch.setattr("litehive.cli.worktree_cli.record_attention", mock_record_attention)

    # Capture stdout
    import io
    import sys
    captured_output = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', captured_output)

    # Run the clean command
    result = clean(workspace=workspace, dry_run=False)

    # Verify the command completed successfully
    assert result == 0

    # Verify output shows successful removal
    output = captured_output.getvalue()
    assert "removed: T-0002-test-task" in output
    assert "removed_count: 1" in output
    assert "deferred_count: 0" in output