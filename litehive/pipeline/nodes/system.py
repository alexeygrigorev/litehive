import subprocess
from abc import abstractmethod
from pathlib import Path
from typing import Callable

from ..events import (
    CleanState,
    Crash,
    Event,
    NeedsPreExecRecovery,
    Pass,
    PreExecRecoverySucceeded,
    Reject,
)
from ..persistence import TaskState
from ..types import NodeName, NodeType
from .base import Node


class MergeConflict(Exception):
    pass


class GitError(Exception):
    pass


class SystemNode(Node):
    node_type = NodeType.SYSTEM

    def __init__(self, name: NodeName) -> None:
        self.name = name

    @abstractmethod
    def run(self, state: TaskState) -> Event: ...


class ReadyNode(SystemNode):
    """Entry probe for a task. Decides between clean entry and pre-exec recovery.

    M1 placeholder: always emits ``CleanState``. Real implementation will
    check for a stale lock file / missing worktree / corrupted state row
    and emit ``NeedsPreExecRecovery`` in those cases.
    """

    def __init__(self) -> None:
        super().__init__("ready")

    def run(self, state: TaskState) -> Event:
        if self._needs_recovery(state):
            return NeedsPreExecRecovery()
        return CleanState()

    def _needs_recovery(self, state: TaskState) -> bool:
        # Hook: subclasses override to detect actual trouble.
        return False


class PreExecRecoveryNode(SystemNode):
    """Runs pre-execution recovery before the task enters the pipeline proper.

    M1 placeholder: always reports success and routes the task to the same
    stage it would have entered on a clean start. Real implementation will
    clear stale locks, abort partial rebases, and repair missing worktrees.
    """

    def __init__(self) -> None:
        super().__init__("recovering_pre_exec")

    def run(self, state: TaskState) -> Event:
        # M1 placeholder: nothing to fix, resume at grooming (full) /
        # implementing (single) via the transition table.
        resume_stage = "implementing" if state.pipeline_mode.value == "single" else "grooming"
        return PreExecRecoverySucceeded(resume_stage=resume_stage)


class CommitNode(SystemNode):
    """Merges the task worktree into main.

    Subclass and override ``_merge_worktree`` to bind to the real git plumbing.
    """

    def __init__(self) -> None:
        super().__init__("commit")

    def run(self, state: TaskState) -> Event:
        try:
            self._merge_worktree(state)
            return Pass()
        except MergeConflict as exc:
            return Reject(source="system", reason=f"merge conflict: {exc}")
        except GitError as exc:
            return Crash(exc_type="GitError", message=str(exc))

    def _merge_worktree(self, state: TaskState) -> None:
        raise NotImplementedError


class StubCommitNode(CommitNode):
    """Always-pass commit node for tests that don't involve real git.

    Returns ``Pass`` unconditionally. Use ``GitCommitNode`` in production.
    """

    def _merge_worktree(self, state: TaskState) -> None:
        return None


WorktreeResolver = Callable[[TaskState], Path]


class GitCommitNode(CommitNode):
    """Real commit node: merge the task worktree into main.

    Execution flow:
      1. Resolve the task's worktree path via ``worktree_resolver(state)``.
      2. Run ``git merge <worktree_branch> --no-edit`` in ``main_repo_root``.
      3. If the merge succeeds → ``Pass``.
      4. If the merge hits conflicts → delegate to ``merge_agent`` (one
         attempt only). If the agent clears ``git diff --diff-filter=U``,
         commit the resolution and return ``Pass``. If conflicts remain,
         abort the merge and return ``Reject``.
      5. Any other git error → ``Crash``.

    The merge agent is injected rather than built here so the state machine
    can reuse the same ``MergeAgent`` singleton across all commit attempts,
    and so tests can pass a stub.
    """

    def __init__(
        self,
        main_repo_root: Path,
        *,
        worktree_resolver: WorktreeResolver,
        merge_agent: "Node | None" = None,
    ) -> None:
        super().__init__()
        self.main_repo_root = Path(main_repo_root)
        self.worktree_resolver = worktree_resolver
        self.merge_agent = merge_agent

    def _merge_worktree(self, state: TaskState) -> None:
        worktree = self.worktree_resolver(state)
        branch_ref = self._worktree_head(worktree)

        result = self._git_merge(branch_ref)
        if result.returncode == 0:
            return

        unresolved = self._unresolved_conflicts()
        if not unresolved:
            raise GitError(
                f"git merge failed with no conflict files: {result.stderr.strip() or result.stdout.strip()}"
            )

        if self.merge_agent is None:
            self._abort_merge()
            raise MergeConflict(
                f"{len(unresolved)} conflicting files and no merge agent configured"
            )

        # Populate failure_context so MergeAgent sees the conflict files in its prompt.
        state.failure_context = {
            **state.failure_context,
            "conflict_files": unresolved,
            "merge_attempt": 1,
        }
        self.merge_agent.run(state)

        remaining = self._unresolved_conflicts()
        if remaining:
            self._abort_merge()
            raise MergeConflict(
                f"merge agent left {len(remaining)} unresolved files: {', '.join(remaining)}"
            )
        # Commit the agent's resolution
        commit = subprocess.run(
            ["git", "commit", "--no-edit"],
            cwd=str(self.main_repo_root),
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            raise GitError(
                f"post-resolve commit failed: {commit.stderr.strip() or commit.stdout.strip()}"
            )

    def _worktree_head(self, worktree: Path) -> str:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise GitError(f"cannot read worktree HEAD at {worktree}: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _git_merge(self, branch_ref: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "merge", branch_ref, "--no-edit"],
            cwd=str(self.main_repo_root),
            capture_output=True,
            text=True,
        )

    def _unresolved_conflicts(self) -> list[str]:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(self.main_repo_root),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def _abort_merge(self) -> None:
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=str(self.main_repo_root),
            capture_output=True,
            text=True,
        )
