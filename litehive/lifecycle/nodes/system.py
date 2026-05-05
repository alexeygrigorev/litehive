import shutil
from abc import abstractmethod
from pathlib import Path
from typing import Callable

from litehive.domain.common import PipelineMode, PipelineState, TaskStage
from litehive.domain.task import TaskRecord
from litehive.git.ops import (
    GitError,
    add_paths,
    check_ignore,
    cherry_check,
    commit_no_edit,
    commit_with_message_stdin,
    current_branch,
    current_head,
    generated_completion_commit_message,
    head_sha_strict,
    is_path_tracked,
    merge_abort,
    merge_no_edit,
    rev_parse_verify,
    status_porcelain_with_options,
    unmerged_files,
)

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
from ..types import NodeType
from .base import Node


class MergeConflict(Exception):
    """Raised by ``CommitNode._merge_worktree`` when git merge leaves files
    in an unresolved state. ``conflict_files`` is the list of paths that
    ``git diff --name-only --diff-filter=U`` reported."""

    def __init__(self, conflict_files: list[str]) -> None:
        super().__init__(f"{len(conflict_files)} unresolved file(s)")
        self.conflict_files = conflict_files


class SystemNode(Node):
    """Base class for pipeline nodes the runner drives without invoking an agent.

    Concrete subclasses cover the non-agent points in the pipeline (entry probe,
    worktree sync, pre-exec recovery, automatic commit) — anywhere we need a
    typed ``Event`` decision but no LLM call.
    """

    node_type = NodeType.SYSTEM

    def __init__(self, name: PipelineState) -> None:
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
    - resumed/requeued worktree up to date with local main → ``Pass``.
    - clean rebase of a resumed/requeued task worktree onto local main → ``Pass``.
    - clean merge of ``origin/main`` into worktree when available → ``Pass``.
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
    the on-disk worktree path for a given task. Existing worktrees for
    requeued/resumed launches are rebased onto the workspace's current local
    HEAD before any agent stage resumes. ``main_ref`` (default ``origin/main``)
    names an optional upstream branch to merge afterward when the task worktree
    still has an ``origin`` remote. When a task does not yet have a recorded
    worktree, the node creates one with a dedicated task branch before any
    agent stage runs.
    """

    def __init__(
        self,
        workspace_root: Path,
        worktree_resolver: "WorktreeResolver",
        main_ref: str = "origin/main",
    ) -> None:
        super().__init__()
        self.workspace_root = Path(workspace_root)
        self.worktree_resolver = worktree_resolver
        self.main_ref = main_ref

    def sync(self, state: TaskState) -> bool:
        from litehive.worktree import WorktreeMergeConflict, WorktreeService  # noqa: PLC0415

        try:
            result = WorktreeService(self.workspace_root).sync_task_worktree(
                state.task_id,
                entry_stage=state.entry_stage,
                worktree_resolver=self.worktree_resolver,
                resolver_state=state,
                main_ref=self.main_ref,
            )
        except WorktreeMergeConflict as exc:
            raise MergeConflict(exc.conflict_files) from exc
        except Exception as exc:
            raise GitError(str(exc)) from exc
        return result.changed


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
                import sys  # noqa: PLC0415

                print(
                    f"[pre-exec repair] ignored error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
        resume_stage = state.entry_stage or (
            TaskStage.IMPLEMENTING.value if state.pipeline_mode == PipelineMode.SINGLE else TaskStage.GROOMING.value
        )
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
        """Subclass hook for the actual merge — ``GitCommitNode`` binds it to git, ``StubCommitNode`` no-ops it."""
        raise NotImplementedError


class StubCommitNode(CommitNode):
    """Always-pass commit node for tests that don't involve real git.

    Returns ``Pass`` unconditionally. Use ``GitCommitNode`` in production.
    """

    def _merge_worktree(self, state: TaskState) -> dict[str, object] | None:
        return None


WorktreeResolver = Callable[[TaskState], Path]
TaskResolver = Callable[[TaskState], TaskRecord | None]


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
    if relpath.startswith(prefix):
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
    return check_ignore(repo_root, relpath)


def _status_entry_needs_git_add(code: str) -> bool:
    """Return whether a porcelain status entry still needs `git add`.

    Staged-only entries such as ``"D "`` already live in the index and can be
    committed directly. Re-running ``git add -- <path>`` on a staged deletion
    whose path no longer exists raises ``pathspec did not match any files``,
    which is what crashed T-0429's commit stage.
    """

    if len(code) >= 1:
        index_state = code[0]
    else:
        index_state = " "
    if len(code) >= 2:
        worktree_state = code[1]
    else:
        worktree_state = " "
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
        worktree_resolver: WorktreeResolver,
        task_resolver: TaskResolver | None = None,
    ) -> None:
        super().__init__()
        self.main_repo_root = Path(main_repo_root)
        self.worktree_resolver = worktree_resolver
        self.task_resolver = task_resolver

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

        merge_ok, merge_message = merge_no_edit(self.main_repo_root, branch_ref)
        if merge_ok:
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
            # Dirty checkout: git refuses to start because local changes
            # would be overwritten.  Parse the affected files and route
            # them to the MergeAgent instead of crashing.
            dirty_files = self._parse_dirty_checkout_files(merge_message)
            if dirty_files:
                raise MergeConflict(dirty_files)
            # Genuine non-conflict failure (bad ref, missing commit, …).
            self._abort_merge()
            raise GitError(f"git merge failed with no conflict files: {merge_message}")

        # Leave the worktree in the unresolved state. The state machine
        # routes MergeConflictDetected → merge_resolving (MergeAgent), which
        # edits the conflicting files in place, runs git add + git commit,
        # and emits Pass. If the agent fails, its prompt instructs it to
        # leave the worktree as-is and report — the recovery agent then
        # decides whether to abort the merge or keep investigating.
        raise MergeConflict(unresolved)

    def main_head(self) -> str | None:
        """Return the current commit on the main repo's HEAD; overridable seam used by the commit-stage merge logic and stubbed in tests."""
        return current_head(self.main_repo_root)

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
        task metadata files (see T-0320).
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
            add_paths(worktree, needs_add)
        message = self._generated_commit_message(state, detail="auto-commit worktree changes")
        commit_with_message_stdin(worktree, message)

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
            add_paths(self.main_repo_root, needs_add, all_flag=True)
        message = self._generated_commit_message(state, detail="auto-commit dirty main checkout")
        commit_with_message_stdin(self.main_repo_root, message)
        head = self.main_head()
        if head is None:
            raise GitError("main cleanup commit completed but main HEAD could not be resolved")
        return head

    def _generated_commit_message(self, state: TaskState, detail: str) -> str:
        """Build the commit message used by the auto-commit helpers — falls back to a plain ``litehive <task_id>`` line when the runner did not wire up a task_resolver."""
        if self.task_resolver is not None:
            task = self.task_resolver(state)
            if task is not None:
                return generated_completion_commit_message(task, detail=detail)
        return f"litehive {state.task_id}: {detail}"

    def git_status_entries(self, repo_root: Path) -> list[tuple[str, str]]:
        """Return ``(porcelain_code, path)`` tuples for the auto-commit helpers; public seam that the commit-stage tests monkey-patch to fake a dirty checkout."""
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
            if is_path_tracked(repo_root, relpath):
                filtered.append(relpath)
        return filtered

    def _git_status_entries_with_options(
        self,
        repo_root: Path,
        include_ignored: bool = False,
    ) -> list[tuple[str, str]]:
        """Parse ``git status --porcelain`` lines for the auto-commit helpers, extracting the destination path on rename/copy entries so ``git add`` can stage them."""
        entries: list[tuple[str, str]] = []
        for line in status_porcelain_with_options(repo_root, include_ignored=include_ignored):
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
        """Snapshot worktree-only paths (gitignored or runner-owned ``.litehive/*.db``) before merge so the commit stage can copy them back onto main afterward — git itself never carries them across the merge."""
        entries = self._git_status_entries_with_options(worktree, include_ignored=True)
        return [path for code, path in entries if code == "!!" or _is_main_checkout_cleanup_excluded(path)]

    def _restore_local_only_paths(self, worktree: Path, relpaths: list[str]) -> None:
        """Copy the local-only paths recorded by ``_worktree_local_only_paths`` from the worktree onto the main checkout once the merge is settled."""
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
        """Return the worktree's HEAD commit; overridable seam the commit-stage tests monkey-patch to simulate already-landed branches."""
        return head_sha_strict(worktree)

    def worktree_branch(self, worktree: Path) -> str | None:
        """Return the branch checked out in the worktree (``None`` for detached HEAD); overridable seam the commit-stage tests stub to control the merge ref."""
        return current_branch(worktree)

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
        return rev_parse_verify(self.main_repo_root, "MERGE_HEAD") is not None

    def _conclude_in_progress_merge(self) -> None:
        commit_no_edit(self.main_repo_root)

    def worktree_patch_already_on_main(self, worktree_head: str, main_head: str | None) -> bool:
        """Return True when every commit on the worktree branch is already on main (e.g. cherry-picked) so the commit stage can short-circuit to a ``reconciled_noop`` instead of producing a duplicate merge."""
        if not main_head:
            return False
        lines = cherry_check(self.main_repo_root, main_head, worktree_head)
        if lines is None:
            return False
        return bool(lines) and all(line.startswith("-") for line in lines)

    def _unresolved_conflicts(self) -> list[str]:
        return unmerged_files(self.main_repo_root)

    def _abort_merge(self) -> None:
        merge_abort(self.main_repo_root)
