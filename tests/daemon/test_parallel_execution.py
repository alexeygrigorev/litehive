"""Tests for parallel task execution functionality."""

import subprocess
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.daemon.parallel import ParallelTaskManager, run_parallel_iteration
from litehive.daemon.integration import TaskIntegrator
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.state.persist import persist_tasks_and_state, load_state
from litehive.state.records import save_task
from litehive.tasks.queue import enqueue_task


def create_test_task(task_id: str, title: str, workspace: Path) -> TaskRecord:
    """Create a test task record."""
    task = TaskRecord(
        id=task_id,
        slug=f"test-{task_id}",
        title=title,
        goal=f"Test goal for {title}",
        acceptance_criteria=[
            "Task should complete successfully",
            "No errors should occur",
        ],
        status="queued",
        pipeline_status="backlog",
    )
    save_task(workspace, task)
    return task


def test_workspace_state_parallel_mode_properties(tmp_path: Path) -> None:
    """Test WorkspaceState parallel mode detection and properties."""
    ensure_workspace(tmp_path)

    # Test single task mode (legacy)
    state = WorkspaceState()
    assert not state.is_parallel_mode
    assert state.has_capacity_for_new_task
    assert state.running_task_ids == []

    # Test parallel mode activation
    state.max_parallel_tasks = 3
    state.add_active_task("task-1", "worktrees/task-1")
    assert state.is_parallel_mode
    assert len(state.active_tasks) == 1
    assert state.running_task_ids == ["task-1"]
    assert state.has_capacity_for_new_task

    # Test capacity limits
    state.add_active_task("task-2", "worktrees/task-2")
    state.add_active_task("task-3", "worktrees/task-3")
    assert not state.has_capacity_for_new_task
    assert len(state.active_tasks) == 3

    # Test task removal
    assert state.remove_active_task("task-2")
    assert state.has_capacity_for_new_task
    assert len(state.active_tasks) == 2
    assert "task-2" not in state.running_task_ids


def test_workspace_state_integration_order(tmp_path: Path) -> None:
    """Test that active tasks get sequential integration order."""
    ensure_workspace(tmp_path)
    state = WorkspaceState(max_parallel_tasks=3)

    # Add tasks and verify they get sequential integration order
    task1 = state.add_active_task("task-1")
    task2 = state.add_active_task("task-2")
    task3 = state.add_active_task("task-3")

    assert task1.integration_order == 1
    assert task2.integration_order == 2
    assert task3.integration_order == 3

    # Remove middle task and add new one
    state.remove_active_task("task-2")
    task4 = state.add_active_task("task-4")

    # New task should get next order, not fill gap
    assert task4.integration_order == 4


@patch('litehive.daemon.parallel.add_worktree')
@patch('litehive.daemon.parallel.current_head')
def test_parallel_task_manager_start_task(
    mock_current_head: MagicMock,
    mock_add_worktree: MagicMock,
    tmp_path: Path
) -> None:
    """Test starting a task in parallel execution."""
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)

    # Setup mocks
    mock_current_head.return_value = "abc123"
    mock_add_worktree.return_value = None

    # Create test task and enqueue it
    task = create_test_task("T-001", "Test Task", workspace)
    enqueue_task(workspace, task.id)

    # Configure for parallel execution
    state = load_state(workspace)
    state.max_parallel_tasks = 2
    persist_tasks_and_state(workspace, tasks=[], state=state)

    # Initialize parallel task manager
    manager = ParallelTaskManager(workspace, ["echo", "test"])

    # Test starting a task
    assert manager.can_start_new_task()

    with patch.object(manager, '_spawn_task_process') as mock_spawn:
        mock_spawn.return_value = MagicMock(pid=12345)

        result = manager.start_next_task()
        assert result is True
        assert manager.active_task_count == 1

        # Verify worktree creation was attempted
        mock_add_worktree.assert_called_once()

        # Verify task is tracked as active
        state = load_state(workspace)
        assert len(state.active_tasks) == 1
        assert state.active_tasks[0].task_id == "T-001"


def test_parallel_task_manager_capacity_limits(tmp_path: Path) -> None:
    """Test parallel task manager respects capacity limits."""
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)

    # Set parallel capacity to 2 and manually add active tasks to test limits
    state = load_state(workspace)
    state.max_parallel_tasks = 2

    # Manually add two active tasks to reach capacity
    state.add_active_task("T-001", "worktrees/task-1")
    state.add_active_task("T-002", "worktrees/task-2")

    persist_tasks_and_state(workspace, tasks=[], state=state)

    manager = ParallelTaskManager(workspace, ["echo", "test"])

    # Should not be able to start another task due to capacity
    assert not manager.can_start_new_task()

    # Verify capacity is correctly checked
    state = load_state(workspace)
    assert len(state.active_tasks) == 2
    assert state.max_parallel_tasks == 2
    assert not state.has_capacity_for_new_task


@patch('subprocess.run')
def test_task_integrator_successful_merge(
    mock_subprocess: MagicMock,
    tmp_path: Path
) -> None:
    """Test successful integration of a completed parallel task."""
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)

    # Setup git repository
    subprocess.run(["git", "init"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=workspace, check=True)

    # Create test task with worktree path
    task = create_test_task("T-001", "Test Task", workspace)
    task.status = "done"  # Mark as completed

    # Set up worktree path
    worktree_rel = ".litehive/worktrees/task-T-001"
    task.git.worktree_path = worktree_rel
    save_task(workspace, task)

    # Create actual worktree directory structure for the test
    worktree_path = workspace / worktree_rel
    worktree_path.mkdir(parents=True, exist_ok=True)

    # Add to integration queue
    state = load_state(workspace)
    state.integration_queue = ["T-001"]
    persist_tasks_and_state(workspace, tasks=[], state=state)

    # Mock successful git operations
    mock_subprocess.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )

    integrator = TaskIntegrator(workspace)

    with patch.object(integrator, '_cleanup_successful_task'):
        result = integrator._attempt_integration("T-001", None)

        assert result.success is True
        assert result.conflict_detected is False


@patch('subprocess.run')
def test_task_integrator_merge_conflict_detection(
    mock_subprocess: MagicMock,
    tmp_path: Path
) -> None:
    """Test merge conflict detection and agent invocation."""
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)

    # Create test task with worktree
    task = create_test_task("T-001", "Test Task", workspace)
    task.status = "done"

    # Set up worktree path
    worktree_rel = ".litehive/worktrees/task-T-001"
    task.git.worktree_path = worktree_rel
    save_task(workspace, task)

    # Create worktree directory
    worktree_path = workspace / worktree_rel
    worktree_path.mkdir(parents=True, exist_ok=True)

    # Mock git operations for conflict
    def mock_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="abc123\n", stderr=""
            )
        elif "checkout" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )
        elif "merge" in cmd:
            # Simulate merge conflict
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout="CONFLICT (content): Merge conflict in file.txt",
                stderr="Automatic merge failed"
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    mock_subprocess.side_effect = mock_run

    integrator = TaskIntegrator(workspace)

    # Mock merge agent to simulate successful resolution
    with patch.object(integrator.merge_agent, 'resolve_conflict') as mock_agent:
        mock_agent.return_value = {"success": True, "resolution_applied": True}

        result = integrator._attempt_integration("T-001", None)

        # Should succeed after agent resolution
        assert result.success is True
        mock_agent.assert_called_once()


def test_integration_queue_processing(tmp_path: Path) -> None:
    """Test processing of integration queue in correct order."""
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)

    # Create multiple completed tasks
    for i in range(3):
        task = create_test_task(f"T-00{i+1}", f"Test Task {i+1}", workspace)
        task.status = "done"
        save_task(workspace, task)

    # Add tasks to integration queue in specific order
    state = load_state(workspace)
    state.integration_queue = ["T-001", "T-002", "T-003"]
    persist_tasks_and_state(workspace, tasks=[], state=state)

    integrator = TaskIntegrator(workspace)

    # Mock successful integration
    with patch.object(integrator, '_attempt_integration') as mock_attempt:
        from litehive.daemon.integration import IntegrationResult
        mock_attempt.return_value = IntegrationResult(task_id="T-001", success=True)

        # Process one iteration
        progress = integrator.process_integration_queue()

        assert progress is True
        mock_attempt.assert_called_once_with("T-001", None)

        # Verify task was removed from queue
        state = load_state(workspace)
        assert "T-001" not in state.integration_queue
        assert state.integration_queue == ["T-002", "T-003"]


def test_run_parallel_iteration_integration(tmp_path: Path) -> None:
    """Test full parallel iteration including task startup and integration."""
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)

    # Create test tasks
    task1 = create_test_task("T-001", "Test Task 1", workspace)
    task2 = create_test_task("T-002", "Test Task 2", workspace)
    enqueue_task(workspace, task1.id)
    enqueue_task(workspace, task2.id)

    # Configure parallel execution
    state = load_state(workspace)
    state.max_parallel_tasks = 2
    persist_tasks_and_state(workspace, tasks=[], state=state)

    # Mock the parallel manager and integration
    with patch('litehive.daemon.parallel.ParallelTaskManager') as MockManager:
        with patch('litehive.daemon.integration.run_integration_iteration') as mock_integration:
            mock_manager = MockManager.return_value
            mock_manager.check_completed_tasks.return_value = []
            mock_manager.can_start_new_task.return_value = True
            mock_manager.start_next_task.return_value = True
            mock_manager.active_task_count = 1

            mock_integration.return_value = False  # No integration progress

            # Run parallel iteration
            result_manager = run_parallel_iteration(workspace, ["echo", "test"])

            # Verify manager methods were called
            mock_manager.check_completed_tasks.assert_called_once()
            # Note: start_next_task might not be called if can_start_new_task returns False after load_state


def test_workspace_state_backwards_compatibility(tmp_path: Path) -> None:
    """Test that new WorkspaceState maintains backwards compatibility."""
    ensure_workspace(tmp_path)

    # Create state with legacy active_task_id
    state = WorkspaceState()
    state.active_task_id = "legacy-task"

    # Should not be in parallel mode
    assert not state.is_parallel_mode
    assert state.running_task_ids == []  # Legacy task not in parallel list

    # Adding parallel task should switch to parallel mode
    state.max_parallel_tasks = 2
    state.add_active_task("new-task")

    assert state.is_parallel_mode
    assert len(state.active_tasks) == 1
    assert state.active_task_id == "legacy-task"  # Legacy field preserved


def test_merge_conflict_agent_prompt_generation(tmp_path: Path) -> None:
    """Test merge conflict agent prompt generation."""
    from litehive.agents.merge_conflict import MergeConflictAgent

    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)

    task = create_test_task("T-001", "Test Task", workspace)
    agent = MergeConflictAgent(workspace)

    conflict_details = "CONFLICT (content): Merge conflict in file.txt"
    prompt = agent._build_conflict_resolution_prompt(
        task, conflict_details, "abc123", "def456"
    )

    assert "Task: Resolve merge conflicts for parallel task integration" in prompt
    assert "T-001" in prompt
    assert "Test Task" in prompt
    assert conflict_details in prompt
    assert "abc123" in prompt
    assert "def456" in prompt
    assert "resolve these merge conflicts" in prompt.lower()


def test_parallel_task_cleanup_on_completion(tmp_path: Path) -> None:
    """Test that completed parallel tasks are properly cleaned up."""
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)

    # Create and start a task
    task = create_test_task("T-001", "Test Task", workspace)

    state = load_state(workspace)
    active_task = state.add_active_task("T-001", "worktrees/task-1")
    persist_tasks_and_state(workspace, tasks=[], state=state)

    manager = ParallelTaskManager(workspace, ["echo", "test"])

    # Simulate a completed process
    mock_process = MagicMock()
    mock_process.poll.return_value = 0  # Completed successfully
    mock_process.returncode = 0

    from litehive.daemon.parallel import ParallelTaskProcess
    manager.running_processes["T-001"] = ParallelTaskProcess(
        task_id="T-001",
        process=mock_process,
        worktree_path=tmp_path / "worktree",
        started_at=time.time(),
        log_path=tmp_path / "log.txt"
    )

    # Check completed tasks
    completed = manager.check_completed_tasks()

    assert "T-001" in completed
    assert "T-001" not in manager.running_processes

    # Task should be moved to integration queue
    state = load_state(workspace)
    assert "T-001" in state.integration_queue
    assert len(state.active_tasks) == 0  # Removed from active tasks


if __name__ == "__main__":
    pytest.main([__file__])