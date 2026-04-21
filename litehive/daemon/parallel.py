"""Parallel task execution management for litehive daemon.

This module handles spawning and coordinating multiple independent task executions,
each in their own isolated worktree. It manages the lifecycle from task dequeue
through execution completion and integration readiness.
"""

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, List, TextIO

from litehive.config.loading import load_config
from litehive.domain.task import TaskRecord
from litehive.git.ops import add_worktree, current_head
from litehive.state.persist import load_state, persist_tasks_and_state
from litehive.state.records import set_task_worktree_path
from litehive.tasks.queue import dequeue_next_task
from litehive.worktree import task_worktree_branch

logger = logging.getLogger(__name__)


@dataclass
class ParallelTaskProcess:
    """Represents a running parallel task execution process."""
    task_id: str
    process: subprocess.Popen[str]
    worktree_path: Path
    started_at: datetime
    log_path: Path


class ParallelTaskManager:
    """Manages multiple concurrent task executions with isolated worktrees."""

    def __init__(self, workspace: Path, command_prefix: List[str]) -> None:
        self.workspace = workspace.resolve()
        self.command_prefix = command_prefix
        self.running_processes: Dict[str, ParallelTaskProcess] = {}

    def can_start_new_task(self) -> bool:
        """Check if we can start a new parallel task based on workspace capacity."""
        state = load_state(self.workspace)
        return state.has_capacity_for_new_task and bool(state.queue)

    def start_next_task(self, output_stream: TextIO | None = None) -> bool:
        """Start the next available task in parallel. Returns True if a task was started."""
        if not self.can_start_new_task():
            return False

        try:
            # Dequeue the next task
            task = dequeue_next_task(self.workspace)
            if task is None:
                return False

            # Set up isolated worktree for this task
            worktree_path = self._create_task_worktree(task)
            if worktree_path is None:
                self._emit(f"Failed to create worktree for task {task.id}", stream=output_stream)
                return False

            # Update workspace state to track this active task
            state = load_state(self.workspace)
            active_task = state.add_active_task(task.id, str(worktree_path.relative_to(self.workspace)))

            # Update task record with worktree path
            set_task_worktree_path(task, str(worktree_path.relative_to(self.workspace)))

            # Save state with the new active task
            persist_tasks_and_state(
                self.workspace,
                tasks=[task],
                state=state,
            )

            # Start the task execution process
            process = self._spawn_task_process(task, worktree_path, output_stream)
            if process is None:
                # Clean up on failure
                state.remove_active_task(task.id)
                persist_tasks_and_state(self.workspace, tasks=[], state=state)
                return False

            # Track the running process
            self.running_processes[task.id] = ParallelTaskProcess(
                task_id=task.id,
                process=process,
                worktree_path=worktree_path,
                started_at=datetime.now(UTC),
                log_path=self._task_log_path(task.id),
            )

            self._emit(f"Started parallel task: {task.id} in {worktree_path}", stream=output_stream)
            return True

        except Exception as exc:
            logger.exception("Failed to start parallel task")
            self._emit(f"Failed to start parallel task: {exc}", stream=output_stream)
            return False

    def check_completed_tasks(self, output_stream: TextIO | None = None) -> List[str]:
        """Check for completed parallel tasks and move them to integration queue."""
        completed_task_ids: List[str] = []

        for task_id, proc_info in list(self.running_processes.items()):
            if proc_info.process.poll() is not None:
                # Process has completed
                return_code = proc_info.process.returncode
                completed_task_ids.append(task_id)

                self._emit(
                    f"Parallel task completed: {task_id} (rc={return_code})",
                    stream=output_stream
                )

                # Remove from running processes
                del self.running_processes[task_id]

                # Move to integration queue if successful
                self._handle_completed_task(task_id, return_code, output_stream)

        return completed_task_ids

    def terminate_all(self) -> None:
        """Terminate all running parallel task processes."""
        for proc_info in self.running_processes.values():
            if proc_info.process.poll() is None:
                proc_info.process.terminate()

        # Wait for termination with timeout
        deadline = time.time() + 10  # 10 second timeout
        while time.time() < deadline and self.running_processes:
            for task_id, proc_info in list(self.running_processes.items()):
                if proc_info.process.poll() is not None:
                    del self.running_processes[task_id]
            time.sleep(0.1)

        # Force kill any remaining processes
        for proc_info in self.running_processes.values():
            if proc_info.process.poll() is None:
                proc_info.process.kill()

        self.running_processes.clear()

    @property
    def active_task_count(self) -> int:
        """Get the number of currently running parallel tasks."""
        return len(self.running_processes)

    def _create_task_worktree(self, task: TaskRecord) -> Path | None:
        """Create an isolated worktree for the task."""
        try:
            # Get the current main branch HEAD for consistent base
            base_sha = current_head(self.workspace)
            if not base_sha:
                logger.error("Could not determine current HEAD for worktree creation")
                return None

            # Generate worktree path and branch name
            worktree_name = f"task-{task.id}"
            worktree_path = self.workspace / ".litehive" / "worktrees" / worktree_name
            branch_name = task_worktree_branch(task)

            # Create the worktree
            add_worktree(
                self.workspace,
                worktree_path,
                branch_name,
                base_ref=base_sha
            )

            return worktree_path

        except Exception as exc:
            logger.exception(f"Failed to create worktree for task {task.id}: {exc}")
            return None

    def _spawn_task_process(
        self,
        task: TaskRecord,
        worktree_path: Path,
        output_stream: TextIO | None
    ) -> subprocess.Popen[str] | None:
        """Spawn a task execution process in the isolated worktree."""
        try:
            log_path = self._task_log_path(task.id)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            # Build the command to run the task in its worktree
            command = [
                *self.command_prefix,
                "run",
                "--workspace", str(self.workspace),
            ]

            # Set up environment for the subprocess
            env = os.environ.copy()
            env["LITEHIVE_TASK_ID"] = task.id
            env["LITEHIVE_WORKSPACE_ROOT"] = str(self.workspace)

            # Start the process
            process = subprocess.Popen(
                command,
                cwd=worktree_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env
            )

            # Start a background thread to log output
            self._start_output_logger(process, log_path)

            return process

        except Exception as exc:
            logger.exception(f"Failed to spawn process for task {task.id}: {exc}")
            return None

    def _start_output_logger(self, process: subprocess.Popen[str], log_path: Path) -> None:
        """Start background logging of process output."""
        import threading

        def log_output():
            try:
                with log_path.open("w", encoding="utf-8") as log_file:
                    if process.stdout:
                        for line in process.stdout:
                            log_file.write(line)
                            log_file.flush()
            except Exception as exc:
                logger.exception(f"Error logging process output: {exc}")

        thread = threading.Thread(target=log_output, daemon=True)
        thread.start()

    def _handle_completed_task(
        self,
        task_id: str,
        return_code: int,
        output_stream: TextIO | None
    ) -> None:
        """Handle a completed parallel task by moving it to integration queue."""
        try:
            state = load_state(self.workspace)

            if return_code == 0:
                # Successful completion - add to integration queue
                if task_id not in state.integration_queue:
                    state.integration_queue.append(task_id)
                    self._emit(f"Task {task_id} ready for integration", stream=output_stream)
            else:
                # Failed execution - remove from active and handle as failed
                self._emit(f"Task {task_id} failed with return code {return_code}", stream=output_stream)

            # Remove from active tasks
            state.remove_active_task(task_id)

            # Save the updated state
            persist_tasks_and_state(self.workspace, tasks=[], state=state)

        except Exception as exc:
            logger.exception(f"Error handling completed task {task_id}: {exc}")

    def _task_log_path(self, task_id: str) -> Path:
        """Get the log file path for a parallel task."""
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.workspace / ".litehive" / "logs" / "parallel" / f"{task_id}-{timestamp}.log"

    def _emit(self, message: str, *, stream: TextIO | None) -> None:
        """Emit a message to the output stream."""
        if stream is None:
            return
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        stream.write(f"[{timestamp}] {message}\n")
        stream.flush()


def run_parallel_iteration(
    workspace: Path,
    command_prefix: List[str],
    manager: ParallelTaskManager | None = None,
    output_stream: TextIO | None = None,
) -> ParallelTaskManager:
    """Run one iteration of parallel task management.

    This function:
    1. Checks for completed tasks and moves them to integration
    2. Starts new tasks if capacity allows
    3. Returns the manager for continued use
    """
    if manager is None:
        manager = ParallelTaskManager(workspace, command_prefix)

    # Check for completed tasks first
    completed = manager.check_completed_tasks(output_stream)
    if completed:
        manager._emit(f"Processed {len(completed)} completed parallel tasks", stream=output_stream)

    # Start new tasks if we have capacity
    config = load_config(workspace)
    max_parallel = getattr(config, 'max_parallel_tasks', 1)

    # Update workspace max parallel tasks if configured
    state = load_state(workspace)
    if state.max_parallel_tasks != max_parallel:
        state.max_parallel_tasks = max_parallel
        persist_tasks_and_state(workspace, tasks=[], state=state)

    started_count = 0
    while manager.can_start_new_task() and manager.active_task_count < max_parallel:
        if manager.start_next_task(output_stream):
            started_count += 1
        else:
            break

    if started_count > 0:
        manager._emit(f"Started {started_count} new parallel tasks", stream=output_stream)

    return manager