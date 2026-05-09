"""Workspace state persistence and atomic write helpers."""

import gzip
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from litehive.domain.common import PipelineState, utcnow
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.state.locking import WorkspaceMutationGuard, WorkspaceStateLock
from litehive.state.store import RuntimeStore
from litehive.tasks.audit import TaskAuditEntry
from litehive.workspace import Workspace

_MISSING = object()
_SKIP_BOOTSTRAP_LOAD_STATE: ContextVar[bool] = ContextVar(
    "litehive_skip_bootstrap_load_state",
    default=False,
)
CONSECUTIVE_TASK_FAILURE_LIMIT = 3
CONSECUTIVE_TASK_FAILURE_STOP_REASON = "consecutive_task_failures"


class WorkspaceStateRepository:
    """
    Workspace-bound persistence API for workspace queue/pool state.

    Owns the state reads and mutations that operate on ``WorkspaceState``.
    Narrow domain methods live here instead of as workspace-first free
    functions, while low-level atomic file helpers remain module utilities.
    """

    def __init__(self, workspace: Workspace, runtime_store: RuntimeStore | None = None) -> None:
        self.workspace = workspace
        self.runtime_store = runtime_store or RuntimeStore(workspace)

    def load(self, bootstrap: bool = True) -> WorkspaceState:
        """
        Return the workspace state for an existing Litehive workspace.
        """
        if bootstrap and not _SKIP_BOOTSTRAP_LOAD_STATE.get():
            self.workspace.require_existing(source="load_state")
        state = self.runtime_store.load_workspace_state()
        if state is None:
            state = WorkspaceState()
            self.runtime_store.save_workspace_state(state)
        return state

    def save(self, state: WorkspaceState) -> None:
        """
        Persist workspace state under the mutation guard.
        """
        with WorkspaceMutationGuard(self.workspace).hold():
            self.runtime_store.save_workspace_state(state)

    def save_without_runner_guard(
        self,
        state: WorkspaceState,
        audit_entries: list[TaskAuditEntry] | None = None,
    ) -> None:
        """
        Persist workspace state assuming the caller already holds the runner guard.
        """
        if audit_entries:
            self.runtime_store.save_runtime_transaction(
                workspace_state=state,
                audit_entries=audit_entries,
            )
            return
        self.runtime_store.save_workspace_state(state)

    def set_pool_stop_reason(self, stop_reason: str | None) -> WorkspaceState:
        """
        Set or clear the pool's stop reason.

        Clearing the consecutive-failure stop also resets the counter so
        operators don't have to chase two fields when resuming the pool; a
        leftover counter would re-trigger the same stop after one more
        failure.
        """
        with WorkspaceStateLock(self.workspace).hold():
            state = self.load()
            if stop_reason is None and state.pool_stop_reason == CONSECUTIVE_TASK_FAILURE_STOP_REASON:
                state.consecutive_task_failures = 0
            state.pool_stop_reason = stop_reason
            self.save_without_runner_guard(state)
            return state

    def record_task_completion(self, final_stage: PipelineState | None) -> tuple[int, str | None]:
        """
        Update the consecutive-failure counter and trigger pool stop at the limit.

        Called by the runner after each task finishes so a streak of failures
        halts the pool instead of grinding through every task; without this
        gate a misconfigured environment would burn through the whole queue
        before an operator noticed.
        """
        with WorkspaceStateLock(self.workspace).hold():
            state = self.load()
            if final_stage == PipelineState.DONE:
                state.consecutive_task_failures = 0
            else:
                state.consecutive_task_failures = max(0, int(state.consecutive_task_failures)) + 1
                if state.consecutive_task_failures >= CONSECUTIVE_TASK_FAILURE_LIMIT:
                    state.pool_stop_reason = CONSECUTIVE_TASK_FAILURE_STOP_REASON
            self.save_without_runner_guard(state)
            return state.consecutive_task_failures, state.pool_stop_reason

    def merged_state_for_runner_owned_write(
        self,
        state: WorkspaceState,
        protected_task_ids: list[str] | tuple[str, ...] = (),
    ) -> WorkspaceState:
        """
        Rebase in-memory workspace state onto whatever the persisted state shows.

        Preserves the runner's edits to protected tasks while picking up
        concurrent CLI changes (queue reorders, future-task additions). The
        rebase prevents the runner from clobbering operator edits that
        landed while a task was in flight.
        """
        latest_state = self.load()
        merged_state = state.model_copy(deep=True)
        merged_state.queue = _merge_queue_preserving_future_changes(
            desired_queue=state.queue,
            latest_queue=latest_state.queue,
            protected_task_ids=protected_task_ids,
        )
        merged_state.next_task_number = max(state.next_task_number, latest_state.next_task_number)
        return merged_state

    def persist_task_and_state(
        self,
        task: TaskRecord,
        state: WorkspaceState,
        journal_message: str | None = None,
        protected_task_ids: list[str] | tuple[str, ...] = (),
        audit_entries: list[TaskAuditEntry] | None = None,
    ) -> None:
        """
        Single-task convenience over ``persist_tasks_and_state``.
        """
        if journal_message is not None:
            journal_messages: dict[str, str] | None = {task.id: journal_message}
        else:
            journal_messages = None
        self.persist_tasks_and_state(
            tasks=[task],
            state=state,
            journal_messages=journal_messages,
            protected_task_ids=protected_task_ids,
            audit_entries=audit_entries,
        )

    def persist_tasks_and_state(
        self,
        tasks: list[TaskRecord] | tuple[TaskRecord, ...],
        state: WorkspaceState,
        journal_messages: dict[str, str] | None = None,
        protected_task_ids: list[str] | tuple[str, ...] = (),
        audit_entries: list[TaskAuditEntry] | None = None,
    ) -> None:
        """
        Atomic write of tasks plus workspace state under the mutation guard.
        """
        with WorkspaceMutationGuard(self.workspace).hold():
            self._persist_tasks_and_state_without_runner_guard(
                tasks=tasks,
                state=state,
                journal_messages=journal_messages,
                protected_task_ids=protected_task_ids,
                audit_entries=audit_entries,
            )

    def persist_tasks_and_state_without_runner_guard(
        self,
        tasks: list[TaskRecord] | tuple[TaskRecord, ...],
        state: WorkspaceState,
        journal_messages: dict[str, str] | None = None,
        protected_task_ids: list[str] | tuple[str, ...] = (),
        audit_entries: list[TaskAuditEntry] | None = None,
    ) -> None:
        """
        Atomic write of tasks plus workspace state assuming the guard is held.
        """
        self._persist_tasks_and_state_without_runner_guard(
            tasks=tasks,
            state=state,
            journal_messages=journal_messages,
            protected_task_ids=protected_task_ids,
            audit_entries=audit_entries,
        )

    def persist_task_and_state_without_runner_guard(
        self,
        task: TaskRecord,
        state: WorkspaceState,
        journal_message: str | None = None,
        protected_task_ids: list[str] | tuple[str, ...] = (),
        audit_entries: list[TaskAuditEntry] | None = None,
    ) -> None:
        """
        Single-task variant of ``persist_tasks_and_state_without_runner_guard``.
        """
        if journal_message is not None:
            journal_messages: dict[str, str] | None = {task.id: journal_message}
        else:
            journal_messages = None
        self.persist_tasks_and_state_without_runner_guard(
            tasks=[task],
            state=state,
            journal_messages=journal_messages,
            protected_task_ids=protected_task_ids,
            audit_entries=audit_entries,
        )

    def _persist_tasks_and_state_without_runner_guard(
        self,
        tasks: list[TaskRecord] | tuple[TaskRecord, ...],
        state: WorkspaceState,
        journal_messages: dict[str, str] | None = None,
        protected_task_ids: list[str] | tuple[str, ...] = (),
        audit_entries: list[TaskAuditEntry] | None = None,
    ) -> None:
        # inline: state.records top-level-imports state.persist (would cycle).
        from litehive.state.records import task_state_for_storage, WorkspaceTasks  # noqa: PLC0415

        for task in tasks:
            task.updated_at = utcnow()
        merged_state = self.merged_state_for_runner_owned_write(
            state=state,
            protected_task_ids=[*protected_task_ids, *[task.id for task in tasks]],
        )
        self.runtime_store.save_runtime_transaction(
            task_intents={task.id: task.to_intent_record() for task in tasks},
            task_states={task.id: task_state_for_storage(task) for task in tasks},
            workspace_state=merged_state,
            task_journal_messages=journal_messages,
            audit_entries=audit_entries,
        )
        WorkspaceTasks(self.workspace).ensure_runtime_ignored()


@contextmanager
def skip_bootstrap_load_state():
    """
    Suppress workspace validation inside ``load_state``.

    Entered by recovery and inspection paths that must read state without
    requiring a complete workspace on disk; without it those flows would
    fail before they can inspect the missing or corrupt files they are
    trying to diagnose.
    """
    token = _SKIP_BOOTSTRAP_LOAD_STATE.set(True)
    try:
        yield
    finally:
        _SKIP_BOOTSTRAP_LOAD_STATE.reset(token)


def atomic_write_text(path: Path, content: str) -> None:
    """
    Write a file via tmp+rename with fsync so partial writes never appear.

    Readers see either the previous content or the new one, never a
    half-written file. The fsync is suppressed under
    ``LITEHIVE_SKIP_FSYNC`` so the test suite stays fast without losing
    crash safety in production.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            if not os.environ.get("LITEHIVE_SKIP_FSYNC"):
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def atomic_write_gzip_text(path: Path, content: str) -> None:
    """
    Atomic-write variant for gzipped artifacts.

    Used by subagent transcripts and prompt dumps; keeps the tmp+rename
    semantics so a half-written ``.gz`` file never appears on disk and
    consumers don't have to guard against truncated archives.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with gzip.open(temp_path, "wt", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _file_snapshot(path: Path) -> str | object:
    """
    Capture the current content of ``path`` as a rollback baseline.

    Returns the ``_MISSING`` sentinel when the file does not exist so the
    rollback step can re-create the absent state by deleting the path
    instead of writing back ``""`` and leaving an empty file.
    """
    if path.exists():
        return path.read_text(encoding="utf-8")
    return _MISSING


def write_atomic_files(writes: dict[Path, str]) -> None:
    """
    Write a batch of files all-or-nothing.

    On any failure, rolls back already-applied writes from in-memory
    snapshots so a partial multi-file update is never observable;
    consumers reading any subset of these paths after the call always see
    a coherent before-or-after picture.
    """
    snapshots = {path: _file_snapshot(path) for path in writes}
    applied: list[Path] = []
    try:
        for path, content in writes.items():
            atomic_write_text(path, content)
            applied.append(path)
    except Exception:
        for path in reversed(applied):
            previous = snapshots[path]
            if previous is _MISSING:
                if path.exists():
                    path.unlink()
                continue
            assert isinstance(previous, str)
            atomic_write_text(path, previous)
        raise


def write_atomic_files_and_then(writes: dict[Path, str], callback) -> None:
    """
    Same all-or-nothing batch write as ``write_atomic_files`` plus a callback.

    The callback is part of the transaction: if it raises, the file
    writes are rolled back too. Used when a downstream side effect (e.g.
    a SQLite commit) must succeed atomically with the file writes — the
    DB transaction fires only after every file lands.
    """
    snapshots = {path: _file_snapshot(path) for path in writes}
    applied: list[Path] = []
    try:
        for path, content in writes.items():
            atomic_write_text(path, content)
            applied.append(path)
        callback()
    except Exception:
        for path in reversed(applied):
            previous = snapshots[path]
            if previous is _MISSING:
                if path.exists():
                    path.unlink()
                continue
            assert isinstance(previous, str)
            atomic_write_text(path, previous)
        raise


def _merge_queue_preserving_future_changes(
    desired_queue: list[str],
    latest_queue: list[str],
    protected_task_ids: list[str] | tuple[str, ...],
) -> list[str]:
    """
    Splice protected task ids into the latest persisted queue.

    Used by the persist path when another writer (CLI ``queue add`` etc.)
    has reordered the queue between the time the runner read state and
    when it tries to write back: the runner only protects ids it just
    promoted, so unrelated concurrent edits survive rather than being
    overwritten by a stale copy.
    """
    protected: list[str] = []
    seen_protected: set[str] = set()
    for task_id in protected_task_ids:
        if task_id in seen_protected:
            continue
        seen_protected.add(task_id)
        protected.append(task_id)
    if not protected:
        return list(desired_queue)

    protected_set = set(protected)
    latest_unprotected = [task_id for task_id in latest_queue if task_id not in protected_set]
    protected_positions = [
        (
            sum(1 for preceding in desired_queue[:index] if preceding not in protected_set),
            task_id,
        )
        for index, task_id in enumerate(desired_queue)
        if task_id in protected_set
    ]
    if not protected_positions:
        return list(latest_unprotected)

    merged = list(latest_unprotected)
    inserted = 0
    inserted_ids: set[str] = set()
    for unprotected_before, task_id in protected_positions:
        if task_id in inserted_ids:
            continue
        insertion_index = min(unprotected_before + inserted, len(merged))
        merged.insert(insertion_index, task_id)
        inserted_ids.add(task_id)
        inserted += 1
    return merged
