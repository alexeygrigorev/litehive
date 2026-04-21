"""Merge conflict resolution agent for parallel task integration.

This module provides specialized agent support for resolving merge conflicts
that arise during parallel task integration. It creates a dedicated agent
that can analyze conflicts and resolve them systematically.
"""

import logging
import subprocess
from pathlib import Path
from typing import Dict, Any

from litehive.domain.task import TaskRecord
from litehive.agents.manager import SubagentManager
from litehive.state.records import get_task

logger = logging.getLogger(__name__)


class MergeConflictAgent:
    """Specialized agent for resolving merge conflicts during parallel task integration."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def resolve_conflict(
        self,
        task_id: str,
        conflict_details: str,
        base_sha: str,
        task_sha: str
    ) -> Dict[str, Any]:
        """
        Invoke the merge conflict resolution agent for a specific task conflict.

        Args:
            task_id: ID of the task that has merge conflicts
            conflict_details: Details about the merge conflict
            base_sha: SHA of the base commit (main branch)
            task_sha: SHA of the task commit that conflicts

        Returns:
            Dictionary with resolution result and details
        """
        try:
            task = get_task(self.workspace, task_id)
            if task is None:
                return {
                    "success": False,
                    "error": f"Task {task_id} not found"
                }

            # Create a specialized prompt for merge conflict resolution
            prompt = self._build_conflict_resolution_prompt(
                task, conflict_details, base_sha, task_sha
            )

            # Create temporary task record for the merge agent
            merge_task = self._create_merge_agent_task(task, conflict_details)

            # Use SubagentManager to run the merge conflict resolution
            manager = SubagentManager(
                task_record=merge_task,
                workspace_root=self.workspace,
                stage="merge_resolving",
            )

            # Run the agent with the conflict resolution prompt
            result = manager.run_agent(
                prompt=prompt,
                role="merge_resolver",
                timeout_seconds=1800,  # 30 minutes for complex conflicts
                workspace_context="",
            )

            if result.verdict == "pass":
                return {
                    "success": True,
                    "resolution_applied": True,
                    "agent_feedback": result.feedback,
                }
            else:
                return {
                    "success": False,
                    "error": f"Merge conflict agent failed: {result.feedback}",
                    "requires_manual_intervention": True,
                }

        except Exception as exc:
            logger.exception(f"Error in merge conflict resolution for task {task_id}")
            return {
                "success": False,
                "error": str(exc),
                "requires_manual_intervention": True,
            }

    def _build_conflict_resolution_prompt(
        self,
        task: TaskRecord,
        conflict_details: str,
        base_sha: str,
        task_sha: str
    ) -> str:
        """Build the prompt for the merge conflict resolution agent."""
        return f"""Task: Resolve merge conflicts for parallel task integration

## Context

You are a specialized merge conflict resolution agent. A parallel task has completed successfully but cannot be automatically integrated into the main branch due to merge conflicts.

**Task being integrated:**
- Task ID: {task.id}
- Title: {task.title}
- Goal: {task.goal}

**Conflict Information:**
- Base commit (main): {base_sha}
- Task commit: {task_sha}
- Conflict details:
```
{conflict_details}
```

## Your Role

Your job is to resolve these merge conflicts by:
1. Understanding the intent of both the main branch changes and the task changes
2. Carefully resolving conflicts to preserve both sets of changes where possible
3. Testing that the resolution maintains functionality
4. Committing the resolved merge

## Instructions

1. **Analyze the conflict**: Examine the conflicting files to understand what each side changed and why
2. **Resolve conflicts**: Edit the conflicting files to integrate both changes appropriately
3. **Test the resolution**: Run any relevant tests to ensure the merge doesn't break functionality
4. **Commit the merge**: Complete the merge with an appropriate commit message

## Acceptance Criteria

- All merge conflicts are resolved (no conflict markers remain)
- Both the main branch changes and task changes are preserved where possible
- Any tests pass successfully
- The merge commit has been created successfully
- The changes maintain the intent of both the original task and main branch modifications

## Constraints

- Do not modify files outside of what's necessary to resolve the conflicts
- Preserve the semantic intent of both change sets
- Use clear, descriptive commit messages for the merge resolution
- If conflicts cannot be safely resolved automatically, flag for manual review

Focus on creating a clean, working resolution that integrates the parallel task changes with the current main branch state.
"""

    def _create_merge_agent_task(self, original_task: TaskRecord, conflict_details: str) -> TaskRecord:
        """Create a specialized task record for the merge conflict agent."""
        merge_task = TaskRecord(
            id=f"{original_task.id}-merge-resolution",
            slug=f"merge-{original_task.slug}",
            title=f"Resolve merge conflicts for {original_task.title}",
            goal=f"Resolve merge conflicts that occurred when integrating task {original_task.id}",
            acceptance_criteria=[
                "All merge conflicts are resolved with no conflict markers remaining",
                "Both main branch and task changes are preserved appropriately",
                "Merge commit is successfully created",
                "No functionality is broken by the conflict resolution"
            ],
            constraints=[
                "Only modify files necessary to resolve conflicts",
                "Preserve semantic intent of both change sets",
                "Use clear commit messages"
            ]
        )
        return merge_task

    def check_resolution_status(self, task_id: str) -> Dict[str, Any]:
        """Check if a merge conflict resolution has completed."""
        try:
            # Check git status to see if merge is still in progress
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                check=True
            )

            # If there are unmerged paths, merge is still in progress
            unmerged_files = [
                line for line in result.stdout.split('\n')
                if line.startswith('UU') or line.startswith('AA') or 'both modified' in line
            ]

            if unmerged_files:
                return {
                    "completed": False,
                    "in_progress": True,
                    "unmerged_files": len(unmerged_files)
                }

            # Check if we're in a merge state
            merge_head_path = self.workspace / ".git" / "MERGE_HEAD"
            if merge_head_path.exists():
                return {
                    "completed": False,
                    "in_progress": True,
                    "status": "merge_in_progress"
                }

            # No merge in progress and no conflicts - merge is complete
            return {
                "completed": True,
                "in_progress": False,
                "status": "resolved"
            }

        except subprocess.CalledProcessError as exc:
            logger.exception("Error checking merge resolution status")
            return {
                "completed": False,
                "in_progress": False,
                "error": str(exc)
            }


def invoke_merge_conflict_agent(
    workspace: Path,
    task_id: str,
    conflict_details: str,
    base_sha: str,
    task_sha: str
) -> Dict[str, Any]:
    """Convenience function to invoke the merge conflict resolution agent."""
    agent = MergeConflictAgent(workspace)
    return agent.resolve_conflict(task_id, conflict_details, base_sha, task_sha)