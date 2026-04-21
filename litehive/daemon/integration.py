"""Deterministic integration of completed parallel tasks.

This module handles the systematic integration of completed parallel tasks
back to the main branch. It processes tasks in their determined integration
order and invokes the merge conflict agent when conflicts arise.
"""

import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from litehive.domain.task import TaskRecord
from litehive.git.ops import current_head, remove_worktree
from litehive.state.persist import load_state, persist_tasks_and_state
from litehive.state.records import get_task, get_task_worktree_path, clear_task_worktree_path, save_task
from litehive.worktree import resolve_recorded_worktree_path
from litehive.agents.merge_conflict import MergeConflictAgent

logger = logging.getLogger(__name__)


@dataclass
class IntegrationResult:
    """Result of attempting to integrate a task."""
    task_id: str
    success: bool
    conflict_detected: bool = False
    error_message: str | None = None
    requires_agent: bool = False


class TaskIntegrator:
    """Manages deterministic integration of completed parallel tasks."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.merge_agent = MergeConflictAgent(workspace)

    def process_integration_queue(self, output_stream: TextIO | None = None) -> bool:
        """Process all tasks in the integration queue. Returns True if progress was made."""
        state = load_state(self.workspace)

        if not state.integration_queue and state.integrating_task_id is None:
            return False

        # If we have a task currently being integrated, check if it's done
        if state.integrating_task_id:
            if self._check_integration_completion(state.integrating_task_id, output_stream):
                # Integration completed, move to next
                state.integrating_task_id = None
                persist_tasks_and_state(self.workspace, tasks=[], state=state)
                return True
            else:
                # Still integrating, nothing more to do this iteration
                return False

        # Start integrating the next task in queue
        if state.integration_queue:
            next_task_id = state.integration_queue[0]
            result = self._attempt_integration(next_task_id, output_stream)

            # Update state based on result
            state = load_state(self.workspace)  # Reload in case it changed

            if result.success:
                # Integration succeeded immediately
                if next_task_id in state.integration_queue:
                    state.integration_queue.remove(next_task_id)
                self._emit(f"Successfully integrated task {next_task_id}", stream=output_stream)
            elif result.requires_agent:
                # Conflict requires agent intervention
                state.integrating_task_id = next_task_id
                if next_task_id in state.integration_queue:
                    state.integration_queue.remove(next_task_id)
                self._emit(
                    f"Task {next_task_id} requires merge conflict agent intervention",
                    stream=output_stream
                )
            else:
                # Integration failed for other reasons
                if next_task_id in state.integration_queue:
                    state.integration_queue.remove(next_task_id)
                self._emit(
                    f"Failed to integrate task {next_task_id}: {result.error_message}",
                    stream=output_stream
                )

            persist_tasks_and_state(self.workspace, tasks=[], state=state)
            return True

        return False

    def _attempt_integration(self, task_id: str, output_stream: TextIO | None) -> IntegrationResult:
        """Attempt to integrate a single task."""
        try:
            task = get_task(self.workspace, task_id)
            if task is None:
                return IntegrationResult(
                    task_id=task_id,
                    success=False,
                    error_message=f"Task {task_id} not found"
                )

            # Get the task's worktree path
            worktree_rel = get_task_worktree_path(task)
            if not worktree_rel:
                return IntegrationResult(
                    task_id=task_id,
                    success=False,
                    error_message=f"Task {task_id} has no associated worktree"
                )

            worktree_path = resolve_recorded_worktree_path(self.workspace, worktree_rel)
            if worktree_path is None or not worktree_path.exists():
                return IntegrationResult(
                    task_id=task_id,
                    success=False,
                    error_message=f"Task {task_id} worktree not found at {worktree_rel}"
                )

            # Check if the task was completed successfully
            if task.status != "done":
                # Task didn't complete successfully, clean up worktree
                self._cleanup_failed_task(task, worktree_path)
                return IntegrationResult(
                    task_id=task_id,
                    success=False,
                    error_message=f"Task {task_id} did not complete successfully (status: {task.status})"
                )

            # Try to merge the task's changes
            return self._merge_task_changes(task, worktree_path, output_stream)

        except Exception as exc:
            logger.exception(f"Error during integration of task {task_id}")
            return IntegrationResult(
                task_id=task_id,
                success=False,
                error_message=str(exc)
            )

    def _merge_task_changes(
        self,
        task: TaskRecord,
        worktree_path: Path,
        output_stream: TextIO | None
    ) -> IntegrationResult:
        """Attempt to merge a task's changes into main."""
        try:
            # Get the commit SHA from the task's worktree
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=True
            )
            task_head_sha = result.stdout.strip()

            # Switch to main branch in workspace
            subprocess.run(
                ["git", "checkout", "main"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                check=True
            )

            # Try to merge the task's changes
            merge_result = subprocess.run(
                ["git", "merge", task_head_sha, "--no-ff", "-m",
                 f"Integrate task {task.id}: {task.title}"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                check=False
            )

            if merge_result.returncode == 0:
                # Merge succeeded
                self._cleanup_successful_task(task, worktree_path)
                self._emit(f"Successfully merged task {task.id}", stream=output_stream)
                return IntegrationResult(task_id=task.id, success=True)
            else:
                # Merge failed - check if it's a conflict
                if "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr:
                    # Conflict detected - invoke agent intervention
                    self._emit(
                        f"Merge conflict detected for task {task.id}, invoking conflict agent",
                        stream=output_stream
                    )

                    # Get commit SHAs for the conflict
                    base_sha = current_head(self.workspace) or "main"

                    # Invoke the merge conflict agent
                    agent_result = self.merge_agent.resolve_conflict(
                        task_id=task.id,
                        conflict_details=f"{merge_result.stdout}\n{merge_result.stderr}",
                        base_sha=base_sha,
                        task_sha=task_head_sha
                    )

                    if agent_result.get("success"):
                        # Agent successfully resolved the conflict
                        self._cleanup_successful_task(task, worktree_path)
                        self._emit(
                            f"Merge conflict agent successfully resolved conflicts for task {task.id}",
                            stream=output_stream
                        )
                        return IntegrationResult(task_id=task.id, success=True)
                    else:
                        # Agent failed to resolve - requires manual intervention
                        return IntegrationResult(
                            task_id=task.id,
                            success=False,
                            conflict_detected=True,
                            requires_agent=False,  # Agent already attempted
                            error_message=f"Merge conflict agent failed: {agent_result.get('error', 'Unknown error')}"
                        )
                else:
                    # Other merge error
                    return IntegrationResult(
                        task_id=task.id,
                        success=False,
                        error_message=f"Merge failed: {merge_result.stderr}"
                    )

        except subprocess.CalledProcessError as exc:
            return IntegrationResult(
                task_id=task.id,
                success=False,
                error_message=f"Git command failed: {exc.stderr if exc.stderr else str(exc)}"
            )
        except Exception as exc:
            return IntegrationResult(
                task_id=task.id,
                success=False,
                error_message=str(exc)
            )

    def _cleanup_successful_task(self, task: TaskRecord, worktree_path: Path) -> None:
        """Clean up resources for a successfully integrated task."""
        try:
            # Remove the worktree
            remove_worktree(self.workspace, worktree_path, force=True)

            # Clear worktree path from task record
            clear_task_worktree_path(task)
            save_task(self.workspace, task)

            # Remove git branch
            subprocess.run(
                ["git", "branch", "-D", f"litehive/task-{task.id}"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                check=False
            )

        except Exception as exc:
            logger.warning(f"Failed to cleanup task {task.id}: {exc}")

    def _cleanup_failed_task(self, task: TaskRecord, worktree_path: Path) -> None:
        """Clean up resources for a failed task."""
        try:
            # Remove the worktree
            remove_worktree(self.workspace, worktree_path, force=True)

            # Clear worktree path from task record
            clear_task_worktree_path(task)
            save_task(self.workspace, task)

            # Remove git branch
            subprocess.run(
                ["git", "branch", "-D", f"litehive/task-{task.id}"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                check=False
            )

        except Exception as exc:
            logger.warning(f"Failed to cleanup failed task {task.id}: {exc}")

    def _check_integration_completion(self, task_id: str, output_stream: TextIO | None) -> bool:
        """Check if a task being integrated by an agent has completed."""
        try:
            # Check the merge conflict agent status
            status = self.merge_agent.check_resolution_status(task_id)

            if status.get("completed"):
                self._emit(f"Merge conflict resolution completed for task {task_id}", stream=output_stream)
                return True
            elif status.get("error"):
                self._emit(
                    f"Merge conflict resolution failed for task {task_id}: {status.get('error')}",
                    stream=output_stream
                )
                return True  # Failed, but completed
            else:
                # Still in progress
                if status.get("unmerged_files"):
                    self._emit(
                        f"Merge conflict resolution in progress for task {task_id} "
                        f"({status.get('unmerged_files')} unmerged files)",
                        stream=output_stream
                    )
                return False

        except Exception as exc:
            logger.exception(f"Error checking integration completion for task {task_id}")
            self._emit(f"Error checking integration status for task {task_id}: {exc}", stream=output_stream)
            return True  # Assume completed on error to avoid infinite loop


    def _emit(self, message: str, *, stream: TextIO | None) -> None:
        """Emit a message to the output stream."""
        if stream is None:
            return
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        stream.write(f"[INTEGRATION] {timestamp} {message}\n")
        stream.flush()


def run_integration_iteration(
    workspace: Path,
    output_stream: TextIO | None = None,
) -> bool:
    """Run one iteration of task integration processing."""
    integrator = TaskIntegrator(workspace)
    return integrator.process_integration_queue(output_stream)