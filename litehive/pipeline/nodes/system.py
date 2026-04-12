import subprocess
from abc import abstractmethod
from pathlib import Path
from typing import Callable

from ..events import (
    CleanState,
    Crash,
    Event,
    MergeConflictDetected,
    NeedsPreExecRecovery,
    Pass,
    PreExecRecoveryBudgetHit,
    PreExecRecoverySucceeded,
    Reject,
)
from ..persistence import TaskState
from ..types import NodeName, NodeType
from .base import Node


class MergeConflict(Exception):
    """Raised by ``CommitNode._merge_worktree`` when git merge leaves files
    in an unresolved state. ``conflict_files`` is the list of paths that
    ``git diff --name-only --diff-filter=U`` reported."""

    def __init__(self, conflict_files: list[str]) -> None:
        super().__init__(f"{len(conflict_files)} unresolved file(s)")
        self.conflict_files = conflict_files


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

    The node takes a list of ``probe`` callables, each of which inspects
    the ``TaskState`` and returns ``True`` when something is broken. If
    any probe fires, ``NeedsPreExecRecovery`` is emitted and the state
    machine routes to ``recovering_pre_exec`` for cleanup. Otherwise
    ``CleanState`` advances the pipeline.

    Callers can register their own probes; the default is an empty list
    (always clean). Production callers typically inject a probe that
    checks the task's recorded worktree_path actually exists on disk.
    """

    def __init__(
        self,
        probes: "list[Callable[[TaskState], bool]] | None" = None,
    ) -> None:
        super().__init__("ready")
        self.probes = list(probes or [])

    def run(self, state: TaskState) -> Event:
        for probe in self.probes:
            try:
                if probe(state):
                    return NeedsPreExecRecovery()
            except Exception:
                # A probe should never crash the pipeline; treat raised
                # exceptions as "needs recovery" so the pre-exec node has
                # a chance to investigate.
                return NeedsPreExecRecovery()
        return CleanState()


class WorktreeSyncNode(SystemNode):
    """Pull main into the task worktree before the pipeline runs.

    Runs after ``ReadyNode`` and before the task's first agent stage. The
    point is to handle tasks that were parked (or otherwise sat idle)
    while main advanced — the agent needs to see the current HEAD of
    main, not whatever main looked like when the task was queued.

    Outcomes:

    - worktree doesn't exist yet (first run of the task) → ``Pass``
      (nothing to sync; the worktree will be created by the SWE flow).
    - worktree up to date with ``origin/main`` → ``Pass``.
    - clean merge of main into worktree → ``Pass``.
    - merge conflict → ``Reject(source="system")`` — the state machine
      routes to ``recovering`` and the recovery agent decides what to
      do (abort, requeue, delegate to merge-resolve, etc.).
    - git error → ``Crash(GitError)`` → recovering via the wildcard
      rule.

    M1 placeholder subclass (``_NoopWorktreeSyncNode``) always returns
    ``Pass`` so the default pipeline doesn't block on this node when
    worktrees aren't wired up. Production callers inject a real
    subclass with a ``worktree_resolver`` callable.
    """

    def __init__(self) -> None:
        super().__init__("worktree_sync")

    def run(self, state: TaskState) -> Event:
        try:
            changed = self._sync(state)
        except MergeConflict as exc:
            return Reject(
                source="system",
                reason=f"worktree_sync merge conflict on {len(exc.conflict_files)} file(s): {', '.join(exc.conflict_files[:5])}",
            )
        except GitError as exc:
            return Crash(exc_type="GitError", message=str(exc))
        # Whether or not main moved, a clean run emits Pass; the rule
        # table routes that to the first stage phase based on mode.
        return Pass()

    def _sync(self, state: TaskState) -> bool:
        """Return True if anything was merged, False if already up-to-date
        or the worktree isn't available yet. Subclasses override to call git."""
        return False


class NoopWorktreeSyncNode(WorktreeSyncNode):
    """Always-pass variant — use when worktrees aren't in play (tests, dry runs)."""

    def _sync(self, state: TaskState) -> bool:
        return False


class GitWorktreeSyncNode(WorktreeSyncNode):
    """Real worktree sync — runs ``git fetch origin`` then ``git merge origin/main``.

    Takes a ``worktree_resolver`` callable that returns the worktree path
    for a given task, and a ``main_ref`` (default ``origin/main``) naming
    the upstream branch to merge from. If the resolved worktree path
    doesn't exist yet (first time a task runs), this is a no-op.
    """

    def __init__(
        self,
        *,
        worktree_resolver: "WorktreeResolver",
        main_ref: str = "origin/main",
    ) -> None:
        super().__init__()
        self.worktree_resolver = worktree_resolver
        self.main_ref = main_ref

    def _sync(self, state: TaskState) -> bool:
        worktree = self.worktree_resolver(state)
        if not Path(worktree).exists():
            return False

        if not self._has_origin(worktree):
            return False

        if self._is_dirty(worktree):
            # Worktree has uncommitted changes — typically the SWE's
            # work-in-progress from a previous run that was interrupted.
            # Merging main into a dirty worktree would fail ("your local
            # changes would be overwritten") or produce a confusing
            # conflict between WIP and main. Skip the sync and let the
            # agent resume on the existing state.
            return False

        fetch = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if fetch.returncode != 0:
            raise GitError(f"git fetch failed: {fetch.stderr.strip() or fetch.stdout.strip()}")

        merge = subprocess.run(
            ["git", "merge", self.main_ref, "--no-edit"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if merge.returncode == 0:
            return "Already up to date" not in merge.stdout

        unresolved = self._unresolved(worktree)
        if unresolved:
            # Leave the worktree in the unresolved state so operator tooling
            # can inspect it; recovery agent decides what to do next.
            raise MergeConflict(unresolved)

        # Merge failed for a non-conflict reason; abort and crash.
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        raise GitError(f"worktree_sync merge failed: {merge.stderr.strip() or merge.stdout.strip()}")

    @staticmethod
    def _is_dirty(worktree: Path) -> bool:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())

    @staticmethod
    def _has_origin(worktree: Path) -> bool:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())

    @staticmethod
    def _unresolved(worktree: Path) -> list[str]:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


class PreExecRecoveryNode(SystemNode):
    """Runs pre-execution recovery before the task enters the pipeline proper.

    Takes a list of ``repair`` callables that each get a chance to fix
    the workspace. Repairs are best-effort: a repair that raises doesn't
    abort the node — it logs to stderr and moves on. When all repairs
    have run, the node emits ``PreExecRecoverySucceeded`` and the state
    machine resumes at the entry stage (``grooming`` for full mode,
    ``implementing`` for single mode).

    If the pre-exec recovery budget is already exhausted, emits
    ``PreExecRecoveryBudgetHit`` instead, which routes to ``failed``.
    """

    def __init__(
        self,
        repairs: "list[Callable[[TaskState], None]] | None" = None,
    ) -> None:
        super().__init__("recovering_pre_exec")
        self.repairs = list(repairs or [])

    def run(self, state: TaskState) -> Event:
        if state.pre_exec_recovery_attempt > 1:
            return PreExecRecoveryBudgetHit()
        for repair in self.repairs:
            try:
                repair(state)
            except Exception as exc:
                import sys

                print(
                    f"[pre-exec repair] ignored error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
        resume_stage = "implementing" if state.pipeline_mode.value == "single" else "grooming"
        return PreExecRecoverySucceeded(resume_stage=resume_stage)


class CommitNode(SystemNode):
    """Automatic git merge — no agents involved.

    Tries to merge the task's worktree branch into main. There are exactly
    three outcomes, which the state machine routes on:

    - clean merge           → ``Pass``
    - merge conflict        → ``MergeConflictDetected(conflict_files=...)``,
                              routed by the rule table to ``merge_resolving``
                              where ``MergeAgent`` takes one shot at cleanup
    - any other git error   → ``Crash``

    Subclass and override ``_merge_worktree`` to bind to real git plumbing.
    The base ``MergeConflict`` exception carries the list of unresolved
    files so the node can surface them in the event.
    """

    def __init__(self) -> None:
        super().__init__("commit")

    def run(self, state: TaskState) -> Event:
        try:
            self._merge_worktree(state)
            return Pass()
        except MergeConflict as exc:
            return MergeConflictDetected(conflict_files=tuple(exc.conflict_files))
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
    """Real ``commit`` node — plain automatic merge, no agents.

    Resolves the task's worktree, runs ``git merge --no-edit``, and:

    - returns on clean merge → ``Pass`` via the base class
    - raises ``MergeConflict(conflict_files)`` on unresolved files → the
      base class converts it to ``MergeConflictDetected`` and the state
      machine routes to ``merge_resolving`` (MergeAgent)
    - raises ``GitError`` on any other failure → ``Crash``

    No merge agent is invoked from this class — that's a separate state
    machine node.
    """

    def __init__(
        self,
        main_repo_root: Path,
        *,
        worktree_resolver: WorktreeResolver,
    ) -> None:
        super().__init__()
        self.main_repo_root = Path(main_repo_root)
        self.worktree_resolver = worktree_resolver

    def _merge_worktree(self, state: TaskState) -> None:
        worktree = self.worktree_resolver(state)
        branch_ref = self._worktree_head(worktree)

        result = self._git_merge(branch_ref)
        if result.returncode == 0:
            # Clean merge or "Already up to date" (which is the case when
            # the task has no dedicated worktree and branch_ref == current
            # HEAD). Either way, commit stage passes.
            return

        unresolved = self._unresolved_conflicts()
        if not unresolved:
            # git merge failed for a reason other than conflicts (e.g. bad
            # ref, missing commit). Leave nothing half-applied.
            self._abort_merge()
            raise GitError(
                f"git merge failed with no conflict files: {result.stderr.strip() or result.stdout.strip()}"
            )

        # Leave the worktree in the unresolved state. The state machine
        # routes MergeConflictDetected → merge_resolving (MergeAgent), which
        # edits the conflicting files in place, runs git add + git commit,
        # and emits Pass. If the agent fails, its prompt instructs it to
        # leave the worktree as-is and report — the recovery agent then
        # decides whether to abort the merge or keep investigating.
        raise MergeConflict(unresolved)

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
