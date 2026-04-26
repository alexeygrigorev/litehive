import shutil
import subprocess
from abc import abstractmethod
from pathlib import Path
from typing import Callable

from litehive.domain.common import PipelineState

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
        super().__init__(PipelineState.READY)
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
        super().__init__(PipelineState.WORKTREE_SYNC)

    def run(self, state: TaskState) -> Event:
        try:
            self.sync(state)
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

    def sync(self, state: TaskState) -> bool:
        """Return True if anything was merged, False if already up-to-date
        or the worktree isn't available yet. Subclasses override to call git."""
        return False


class NoopWorktreeSyncNode(WorktreeSyncNode):
    """Always-pass variant — use when worktrees aren't in play (tests, dry runs)."""

    def sync(self, state: TaskState) -> bool:
        return False


class GitWorktreeSyncNode(WorktreeSyncNode):
    """Real worktree sync — provisions a task worktree, then syncs from ``main``.

    Takes the workspace root plus a ``worktree_resolver`` callable that returns
    the on-disk worktree path for a given task, and a ``main_ref`` (default
    ``origin/main``) naming the upstream branch to merge from. When a task does
    not yet have a recorded worktree, the node creates one with a dedicated task
    branch before any agent stage runs.
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        worktree_resolver: "WorktreeResolver",
        main_ref: str = "origin/main",
    ) -> None:
        super().__init__()
        self.workspace_root = Path(workspace_root)
        self.worktree_resolver = worktree_resolver
        self.main_ref = main_ref

    def sync(self, state: TaskState) -> bool:
        from litehive.state.records import get_task, get_task_worktree_path, save_task, set_task_worktree_path
        from litehive.worktree import (
            ensure_worktree_venv_link,
            resolve_recorded_worktree_path,
            serialize_worktree_path,
            task_worktree_branch,
            task_worktree_path,
        )

        if not self._is_git_repo(self.workspace_root):
            return False

        task = get_task(self.workspace_root, state.task_id)
        if task is None:
            raise GitError(f"task {state.task_id} not found while creating worktree")

        recorded = resolve_recorded_worktree_path(self.workspace_root, get_task_worktree_path(task))
        if recorded is None or not recorded.exists():
            branch = task_worktree_branch(task)
            existing = self.registered_worktree_for_branch(self.workspace_root, branch)
            if existing is not None:
                set_task_worktree_path(task, serialize_worktree_path(existing))
                save_task(self.workspace_root, task)
            else:
                worktree = task_worktree_path(self.workspace_root, task)
                worktree.parent.mkdir(parents=True, exist_ok=True)
                self.prune_stale_worktrees(self.workspace_root)
                created = subprocess.run(
                    ["git", "worktree", "add", "--force", "-B", branch, str(worktree), "HEAD"],
                    cwd=str(self.workspace_root),
                    capture_output=True,
                    text=True,
                )
                if created.returncode != 0:
                    raise GitError(f"git worktree add failed: {created.stderr.strip() or created.stdout.strip()}")
                ensure_worktree_venv_link(self.workspace_root, worktree)
                set_task_worktree_path(task, serialize_worktree_path(worktree))
                save_task(self.workspace_root, task)
                return True

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

        stash_ref = self._stash_local_changes(worktree)
        restored_stash = False
        try:
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
                changed = "Already up to date" not in merge.stdout
                self._restore_local_changes(worktree, stash_ref)
                restored_stash = True
                return changed

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
            self._restore_local_changes(worktree, stash_ref)
            restored_stash = True
            raise GitError(f"worktree_sync merge failed: {merge.stderr.strip() or merge.stdout.strip()}")
        except Exception:
            if stash_ref and not restored_stash and not self._unresolved(worktree):
                self._restore_local_changes(worktree, stash_ref)
            raise

    @staticmethod
    def _is_git_repo(root: Path) -> bool:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    @staticmethod
    def prune_stale_worktrees(root: Path) -> None:
        proc = subprocess.run(
            ["git", "worktree", "prune", "--expire", "now"],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise GitError(f"git worktree prune failed: {proc.stderr.strip() or proc.stdout.strip()}")

    @staticmethod
    def registered_worktree_for_branch(root: Path, branch: str) -> Path | None:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise GitError(f"git worktree list failed: {proc.stderr.strip() or proc.stdout.strip()}")

        current_path: Path | None = None
        current_branch: str | None = None
        for raw_line in proc.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                if current_branch == branch and current_path is not None and current_path.exists():
                    return current_path.resolve()
                current_path = None
                current_branch = None
                continue
            if line.startswith("worktree "):
                current_path = Path(line.removeprefix("worktree ").strip()).expanduser()
                continue
            if not line.startswith("branch refs/heads/"):
                continue
            current_branch = line.removeprefix("branch refs/heads/").strip()

        if current_branch == branch and current_path is not None and current_path.exists():
            return current_path.resolve()
        return None

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

    @staticmethod
    def _stash_local_changes(worktree: Path) -> str | None:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if status.returncode != 0 or not status.stdout.strip():
            return None
        before = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "refs/stash"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        stash = subprocess.run(
            ["git", "stash", "push", "-u", "-m", "litehive-worktree-sync"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if stash.returncode != 0:
            raise GitError(f"git stash push failed: {stash.stderr.strip() or stash.stdout.strip()}")
        after = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "refs/stash"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        before_ref = before.stdout.strip()
        after_ref = after.stdout.strip()
        if not after_ref or after_ref == before_ref:
            return None
        return after_ref

    def _restore_local_changes(self, worktree: Path, stash_ref: str | None) -> None:
        if not stash_ref:
            return
        restored = subprocess.run(
            ["git", "stash", "pop", "--index", stash_ref],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if restored.returncode == 0:
            return
        unresolved = self._unresolved(worktree)
        if unresolved:
            raise MergeConflict(unresolved)
        raise GitError(f"git stash pop failed: {restored.stderr.strip() or restored.stdout.strip()}")


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
        resume_stage = state.entry_stage or ("implementing" if state.pipeline_mode.value == "single" else "grooming")
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
            metadata = self._merge_worktree(state) or {}
            return Pass(metadata=metadata)
        except MergeConflict as exc:
            return MergeConflictDetected(conflict_files=tuple(exc.conflict_files))
        except GitError as exc:
            return Crash(exc_type="GitError", message=str(exc))

    def _merge_worktree(self, state: TaskState) -> dict[str, object] | None:
        raise NotImplementedError


class StubCommitNode(CommitNode):
    """Always-pass commit node for tests that don't involve real git.

    Returns ``Pass`` unconditionally. Use ``GitCommitNode`` in production.
    """

    def _merge_worktree(self, state: TaskState) -> dict[str, object] | None:
        return None


WorktreeResolver = Callable[[TaskState], Path]


def _is_runner_owned_metadata(relpath: str, task_id: str) -> bool:
    """Return True if ``relpath`` is a runner-written workspace metadata file
    that must be excluded from the commit-stage auto-commit.

    The runner rewrites these files in both the worktree and the main repo
    as the pipeline advances; capturing them in the checkpoint commit
    causes ``git merge`` to abort with "Your local changes would be
    overwritten" (see T-0320).

    ``uv.lock`` is also excluded: test runs regenerate its ``exclude-newer``
    timestamp, producing spurious conflicts with main's lockfile on every
    merge. Dependency changes go via ``pyproject.toml``; the merge-resolver
    (or a post-merge ``uv sync``) regenerates a consistent lockfile.
    """
    prefix = f".litehive/tasks/{task_id}-"
    if relpath.startswith(prefix) or relpath.startswith(".litehive/tasks/archive/"):
        return True
    return relpath == "uv.lock"


def _is_main_checkout_cleanup_excluded(relpath: str) -> bool:
    """Return True when ``relpath`` must stay out of the main cleanup commit."""
    return relpath.startswith(".litehive/") and relpath.count("/") == 1 and relpath.endswith(".db")


def _is_ignored_even_if_tracked(repo_root: Path, relpath: str) -> bool:
    """Return whether Git ignore rules exclude ``relpath`` in ``repo_root``.

    ``git add --all -- <path>`` still errors on tracked paths that now match an
    ignore rule (for example old task report artifacts under
    ``.litehive/tasks/*/reports``). ``git check-ignore --no-index`` asks Git to
    evaluate ignore rules for the path regardless of index state so the main
    cleanup commit can skip those runtime artifacts safely.
    """

    proc = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", "--", relpath],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitError(f"git check-ignore failed in {repo_root}: {proc.stderr.strip() or proc.stdout.strip()}")


def _status_entry_needs_git_add(code: str) -> bool:
    """Return whether a porcelain status entry still needs `git add`.

    Staged-only entries such as ``"D "`` already live in the index and can be
    committed directly. Re-running ``git add -- <path>`` on a staged deletion
    whose path no longer exists raises ``pathspec did not match any files``,
    which is what crashed T-0429's commit stage.
    """

    index_state = code[0] if len(code) >= 1 else " "
    worktree_state = code[1] if len(code) >= 2 else " "
    return worktree_state != " " or index_state in {"?", "U"}


def _is_untracked_embedded_git_repo(repo_root: Path, code: str, relpath: str) -> bool:
    """Return True when an untracked path is itself a nested git checkout.

    Agents and verification commands sometimes create disposable repos inside a
    task worktree (for example a literal ``$workspace/`` scratch directory).
    ``git add -- <dir>`` treats that as an embedded repository; if the nested
    repo has no commit yet, Git aborts with "does not have a commit checked
    out". Those scratch repos must stay local to the task worktree and should
    never block the commit stage.
    """

    if code != "??":
        return False
    candidate = repo_root / relpath
    if not candidate.is_dir():
        return False
    git_marker = candidate / ".git"
    return git_marker.is_dir() or git_marker.is_file()


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

    def _merge_worktree(self, state: TaskState) -> dict[str, object] | None:
        worktree = self.worktree_resolver(state)
        self.autocommit_worktree_changes(worktree, state)
        local_only_paths = self._worktree_local_only_paths(worktree)
        main_head_before = self.main_head()
        branch_ref = self.worktree_branch(worktree) or self.worktree_head(worktree)
        worktree_head = self.worktree_head(worktree)

        if worktree_head != main_head_before and self.worktree_patch_already_on_main(
            worktree_head,
            main_head_before,
        ):
            self._restore_local_only_paths(worktree, local_only_paths)
            cleanup_head = self.autocommit_main_checkout_changes(state)
            return {
                "commit_result": {
                    "status": "reconciled_noop",
                    "reason": "already_landed",
                    "head_sha": cleanup_head or main_head_before,
                }
            }

        if self._merge_in_progress():
            unresolved = self._unresolved_conflicts()
            if unresolved:
                raise MergeConflict(unresolved)
            self._conclude_in_progress_merge()
            return None

        result = self.git_merge(branch_ref)
        if result.returncode == 0:
            # Clean merge or "Already up to date" (which is the case when
            # the task has no dedicated worktree and branch_ref == current
            # HEAD). Either way, commit stage passes.
            merge_head_after = self.main_head()
            if merge_head_after is None:
                raise GitError("git merge completed but main HEAD could not be resolved")
            self._restore_local_only_paths(worktree, local_only_paths)
            cleanup_head = self.autocommit_main_checkout_changes(state)
            main_head_after = cleanup_head or merge_head_after
            if main_head_before == merge_head_after:
                reason = "no_op"
                if worktree_head != main_head_before and self.worktree_patch_already_on_main(
                    worktree_head,
                    main_head_before,
                ):
                    reason = "already_landed"
                return {
                    "commit_result": {
                        "status": "reconciled_noop",
                        "reason": reason,
                        "head_sha": main_head_after,
                    }
                }
            return None

        unresolved = self._unresolved_conflicts()
        if not unresolved:
            stderr = result.stderr.strip() or result.stdout.strip()
            # Dirty checkout: git refuses to start because local changes
            # would be overwritten.  Parse the affected files and route
            # them to the MergeAgent instead of crashing.
            dirty_files = self._parse_dirty_checkout_files(stderr)
            if dirty_files:
                raise MergeConflict(dirty_files)
            # Genuine non-conflict failure (bad ref, missing commit, …).
            self._abort_merge()
            raise GitError(f"git merge failed with no conflict files: {stderr}")

        # Leave the worktree in the unresolved state. The state machine
        # routes MergeConflictDetected → merge_resolving (MergeAgent), which
        # edits the conflicting files in place, runs git add + git commit,
        # and emits Pass. If the agent fails, its prompt instructs it to
        # leave the worktree as-is and report — the recovery agent then
        # decides whether to abort the merge or keep investigating.
        raise MergeConflict(unresolved)

    def main_head(self) -> str | None:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(self.main_repo_root),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def autocommit_worktree_changes(self, worktree: Path, state: TaskState) -> None:
        """Commit any uncommitted SWE edits inside the worktree.

        SWE subagents edit files but do not run `git commit`. Without this,
        the worktree branch ref is unchanged from main HEAD and `git merge`
        below is a silent no-op ("Already up to date"), producing an
        empty-pass that loses the agent's work.

        Runner-owned task metadata under ``.litehive/tasks/<task_id>-*/`` is
        excluded: the runner rewrites those files in both the worktree and
        the main repo as the pipeline advances, and capturing them in the
        checkpoint commit causes ``git merge`` to abort with "Your local
        changes would be overwritten" on any task that updates its own
        task.yaml (see T-0320).
        """
        if worktree == self.main_repo_root:
            return
        entries = self.git_status_entries(worktree)
        if not entries:
            return
        committable = [
            (code, path)
            for code, path in entries
            if not _is_runner_owned_metadata(path, state.task_id)
            and not _is_main_checkout_cleanup_excluded(path)
            and not _is_untracked_embedded_git_repo(worktree, code, path)
        ]
        if not committable:
            # Only runner-owned metadata is dirty — nothing to checkpoint.
            return
        needs_add = [path for code, path in committable if _status_entry_needs_git_add(code)]

        if needs_add:
            add = subprocess.run(
                ["git", "add", "--", *needs_add],
                cwd=str(worktree),
                capture_output=True,
                text=True,
            )
            if add.returncode != 0:
                raise GitError(f"git add failed in {worktree}: {add.stderr.strip()}")
        message = f"litehive {state.task_id}: auto-commit worktree changes"
        commit = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            raise GitError(f"git commit failed in {worktree}: {commit.stderr.strip() or commit.stdout.strip()}")

    def autocommit_main_checkout_changes(self, state: TaskState) -> str | None:
        """Commit any remaining dirty non-ignored files on main after a clean merge."""
        entries = self.git_status_entries(self.main_repo_root)
        committable = [
            (code, path)
            for code, path in entries
            if code != "!!"
            and not _is_main_checkout_cleanup_excluded(path)
            and not _is_ignored_even_if_tracked(self.main_repo_root, path)
        ]
        if not committable:
            return None
        needs_add = [path for code, path in committable if _status_entry_needs_git_add(code)]
        needs_add = self._filter_stageable_paths(self.main_repo_root, needs_add)

        if needs_add:
            add = subprocess.run(
                ["git", "add", "--all", "--", *needs_add],
                cwd=str(self.main_repo_root),
                capture_output=True,
                text=True,
            )
            if add.returncode != 0:
                raise GitError(f"git add failed in {self.main_repo_root}: {add.stderr.strip() or add.stdout.strip()}")
        message = f"litehive {state.task_id}: auto-commit dirty main checkout"
        commit = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(self.main_repo_root),
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            raise GitError(
                f"git commit failed in {self.main_repo_root}: {commit.stderr.strip() or commit.stdout.strip()}"
            )
        head = self.main_head()
        if head is None:
            raise GitError("main cleanup commit completed but main HEAD could not be resolved")
        return head

    def git_status_entries(self, repo_root: Path) -> list[tuple[str, str]]:
        return self._git_status_entries_with_options(repo_root)

    def _filter_stageable_paths(self, repo_root: Path, paths: list[str]) -> list[str]:
        """Drop stale pathspecs so one vanished path cannot abort the batch add.

        `git status` and the subsequent `git add` are separate steps. If a path
        disappears in between and is no longer tracked, `git add -- <path>`
        fails the entire command with a pathspec error. Existing files and
        tracked deletions are safe to pass through.
        """
        filtered: list[str] = []
        for relpath in paths:
            candidate = repo_root / relpath
            if candidate.exists() or candidate.is_symlink():
                filtered.append(relpath)
                continue
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", relpath],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
            )
            if tracked.returncode == 0:
                filtered.append(relpath)
        return filtered

    def _git_status_entries_with_options(
        self,
        repo_root: Path,
        *,
        include_ignored: bool = False,
    ) -> list[tuple[str, str]]:
        command = ["git", "status", "--porcelain"]
        if include_ignored:
            command.extend(["--ignored", "--untracked-files=all"])
        status = subprocess.run(
            command,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if status.returncode != 0:
            raise GitError(f"git status failed in {repo_root}: {status.stderr.strip() or status.stdout.strip()}")

        entries: list[tuple[str, str]] = []
        for line in status.stdout.splitlines():
            if not line.strip():
                continue
            # Porcelain format: "XY path" for most changes; "R  old -> new" for
            # renames, "C  old -> new" for copies. Extract the new path for
            # rename/copy entries — `git add -- "old -> new"` is not valid.
            code = line[:2]
            path = line[3:]
            if any(mark in {"R", "C"} for mark in code) and " -> " in path:
                path = path.split(" -> ", 1)[1]
            entries.append((code, path))
        return entries

    def _worktree_local_only_paths(self, worktree: Path) -> list[str]:
        entries = self._git_status_entries_with_options(worktree, include_ignored=True)
        return [path for code, path in entries if code == "!!" or _is_main_checkout_cleanup_excluded(path)]

    def _restore_local_only_paths(self, worktree: Path, relpaths: list[str]) -> None:
        for relpath in relpaths:
            source = worktree / relpath
            if not source.exists():
                continue
            destination = self.main_repo_root / relpath
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
                continue
            shutil.copy2(source, destination)

    def worktree_head(self, worktree: Path) -> str:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise GitError(f"cannot read worktree HEAD at {worktree}: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def worktree_branch(self, worktree: Path) -> str | None:
        proc = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def git_merge(self, branch_ref: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "merge", branch_ref, "--no-edit"],
            cwd=str(self.main_repo_root),
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _parse_dirty_checkout_files(stderr: str) -> list[str]:
        """Extract file paths from a dirty-checkout merge refusal.

        Git prints messages like::

            error: Your local changes to the following files would be overwritten by merge:
                litehive/domain/common.py
                litehive/tasks/queue.py
            Please commit your changes or stash them before you merge.

        Returns the list of affected files, or an empty list if the error
        is not a dirty-checkout refusal.
        """
        if "would be overwritten by merge" not in stderr:
            return []
        files: list[str] = []
        capture = False
        for line in stderr.splitlines():
            stripped = line.strip()
            if "would be overwritten by merge" in stripped:
                capture = True
                continue
            if capture:
                if stripped.startswith("Please ") or stripped.startswith("error:") or not stripped:
                    capture = False
                    continue
                files.append(stripped)
        return files

    def _merge_in_progress(self) -> bool:
        proc = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            cwd=str(self.main_repo_root),
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0

    def _conclude_in_progress_merge(self) -> None:
        commit = subprocess.run(
            ["git", "commit", "--no-edit"],
            cwd=str(self.main_repo_root),
            capture_output=True,
            text=True,
        )
        if commit.returncode == 0:
            return
        raise GitError(
            "git merge is already in progress but could not be concluded: "
            f"{commit.stderr.strip() or commit.stdout.strip()}"
        )

    def worktree_patch_already_on_main(self, worktree_head: str, main_head: str | None) -> bool:
        if not main_head:
            return False
        cherry = subprocess.run(
            ["git", "cherry", main_head, worktree_head],
            cwd=str(self.main_repo_root),
            capture_output=True,
            text=True,
        )
        if cherry.returncode != 0:
            return False
        lines = [line.strip() for line in cherry.stdout.splitlines() if line.strip()]
        return bool(lines) and all(line.startswith("-") for line in lines)

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
