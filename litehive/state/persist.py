"""Workspace state persistence and atomic write helpers."""

import gzip
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.domain.common import PipelineState, utcnow
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.state.locking import workspace_lock, workspace_mutation_guard
from litehive.state.store import runtime_store
from litehive.tasks.audit import TaskAuditEntry

_MISSING = object()
_SKIP_BOOTSTRAP_LOAD_STATE: ContextVar[bool] = ContextVar(
    "litehive_skip_bootstrap_load_state",
    default=False,
)
CONSECUTIVE_TASK_FAILURE_LIMIT = 3
CONSECUTIVE_TASK_FAILURE_STOP_REASON = "consecutive_task_failures"


@contextmanager
def skip_bootstrap_load_state():
    """
    Suppress workspace bootstrap inside ``load_state``.

    Entered by recovery and inspection paths that must read state without
    provisioning workspace files on disk; without it those flows would
    create the very ``.litehive`` directory they are trying to inspect
    for evidence of corruption.
    """
    token = _SKIP_BOOTSTRAP_LOAD_STATE.set(True)
    try:
        yield
    finally:
        _SKIP_BOOTSTRAP_LOAD_STATE.reset(token)


def load_state(root: Path, bootstrap: bool = True) -> WorkspaceState:
    """
    Return the workspace state, materialising an empty row on first read.

    The canonical reader used everywhere the runner, CLI, or dashboards
    need queue/runner pointers; bootstrapping on first read is what makes
    a fresh workspace usable without a separate ``init`` step.
    """
    if bootstrap and not _SKIP_BOOTSTRAP_LOAD_STATE.get():
        ensure_workspace(root)
    store = runtime_store(root)
    state = store.load_workspace_state()
    if state is None:
        state = WorkspaceState()
        store.save_workspace_state(state)
    return state


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


def save_state(root: Path, state: WorkspaceState) -> None:
    """
    Persist workspace state under the mutation guard.

    The path used by CLI commands that change queue/pool settings outside
    an active runner; taking the guard here means a CLI mutation cannot
    race a runner that's also rewriting workspace state.
    """
    with workspace_mutation_guard(root):
        runtime_store(root).save_workspace_state(state)


def save_state_without_runner_guard(
    root: Path,
    state: WorkspaceState,
    audit_entries: list[TaskAuditEntry] | None = None,
) -> None:
    """
    Persist workspace state assuming the caller already holds the runner guard.

    Used on the runner's hot path so nested mutations do not re-acquire
    the same lock and re-trigger guard bookkeeping; the optional audit
    entries land in the same SQLite transaction so observers see the
    workspace and the audit advance together.
    """
    if audit_entries:
        runtime_store(root).save_runtime_transaction(
            workspace_state=state,
            audit_entries=audit_entries,
        )
        return
    runtime_store(root).save_workspace_state(state)


def record_task_completion(root: Path, final_stage: PipelineState | None) -> tuple[int, str | None]:
    """
    Update the consecutive-failure counter and trigger pool stop at the limit.

    Called by the runner after each task finishes so a streak of failures
    halts the pool instead of grinding through every task; without this
    gate a misconfigured environment would burn through the whole queue
    before an operator noticed.
    """
    with workspace_lock(root):
        state = load_state(root)
        if final_stage == PipelineState.DONE:
            state.consecutive_task_failures = 0
        else:
            state.consecutive_task_failures = max(0, int(state.consecutive_task_failures)) + 1
            if state.consecutive_task_failures >= CONSECUTIVE_TASK_FAILURE_LIMIT:
                state.pool_stop_reason = CONSECUTIVE_TASK_FAILURE_STOP_REASON
        save_state_without_runner_guard(root, state)
        return state.consecutive_task_failures, state.pool_stop_reason


def set_pool_stop_reason(root: Path, stop_reason: str | None) -> WorkspaceState:
    """
    Set or clear the pool's stop reason.

    Clearing the consecutive-failure stop also resets the counter so
    operators don't have to chase two fields when resuming the pool; a
    leftover counter would re-trigger the same stop after one more
    failure.
    """
    with workspace_lock(root):
        state = load_state(root)
        if stop_reason is None and state.pool_stop_reason == CONSECUTIVE_TASK_FAILURE_STOP_REASON:
            state.consecutive_task_failures = 0
        state.pool_stop_reason = stop_reason
        save_state_without_runner_guard(root, state)
        return state


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


def merged_state_for_runner_owned_write(
    root: Path,
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
    latest_state = load_state(root)
    merged_state = state.model_copy(deep=True)
    merged_state.queue = _merge_queue_preserving_future_changes(
        desired_queue=state.queue,
        latest_queue=latest_state.queue,
        protected_task_ids=protected_task_ids,
    )
    merged_state.next_task_number = max(state.next_task_number, latest_state.next_task_number)
    return merged_state


def persist_task_and_state(
    root: Path,
    task: TaskRecord,
    state: WorkspaceState,
    journal_message: str | None = None,
    protected_task_ids: list[str] | tuple[str, ...] = (),
    audit_entries: list[TaskAuditEntry] | None = None,
) -> None:
    """
    Single-task convenience over ``persist_tasks_and_state``.

    The common case used by stage transitions that mutate exactly one
    task plus the workspace queue; the multi-task variant is preferred
    when several tasks change together so they all land in one
    transaction rather than serialised separate writes.
    """
    if journal_message is not None:
        journal_messages: dict[str, str] | None = {task.id: journal_message}
    else:
        journal_messages = None
    persist_tasks_and_state(
        root,
        tasks=[task],
        state=state,
        journal_messages=journal_messages,
        protected_task_ids=protected_task_ids,
        audit_entries=audit_entries,
    )


def persist_tasks_and_state(
    root: Path,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...],
    state: WorkspaceState,
    journal_messages: dict[str, str] | None = None,
    protected_task_ids: list[str] | tuple[str, ...] = (),
    audit_entries: list[TaskAuditEntry] | None = None,
) -> None:
    """
    Atomic write of tasks plus workspace state under the mutation guard.

    The runner-owned write path: rebases via
    ``merged_state_for_runner_owned_write`` before persisting so
    concurrent operator edits land alongside the runner's changes
    instead of being overwritten by the runner's stale read.
    """
    # inline: state.records top-level-imports state.persist (would cycle).
    from litehive.state.records import ensure_runtime_ignored, task_state_for_storage  # noqa: PLC0415

    for task in tasks:
        task.updated_at = utcnow()
    with workspace_mutation_guard(root):
        merged_state = merged_state_for_runner_owned_write(
            root,
            state=state,
            protected_task_ids=[*protected_task_ids, *[task.id for task in tasks]],
        )
        runtime_store(root).save_runtime_transaction(
            task_intents={task.id: task.to_intent_record() for task in tasks},
            task_states={task.id: task_state_for_storage(task) for task in tasks},
            workspace_state=merged_state,
            task_journal_messages=journal_messages,
            audit_entries=audit_entries,
        )
        ensure_runtime_ignored(root)


def persist_tasks_and_state_without_runner_guard(
    root: Path,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...],
    state: WorkspaceState,
    journal_messages: dict[str, str] | None = None,
    protected_task_ids: list[str] | tuple[str, ...] = (),
    audit_entries: list[TaskAuditEntry] | None = None,
) -> None:
    """
    Atomic write of tasks plus workspace state assuming the guard is held.

    Used inside the runner's hot loop where the guard wraps the whole
    task lifetime; re-entering the guard here would force unnecessary
    bookkeeping and could deadlock against the same thread that is
    already inside ``workspace_runner_guard``.
    """
    # inline: state.records top-level-imports state.persist (would cycle).
    from litehive.state.records import ensure_runtime_ignored, task_state_for_storage  # noqa: PLC0415

    for task in tasks:
        task.updated_at = utcnow()
    merged_state = merged_state_for_runner_owned_write(
        root,
        state=state,
        protected_task_ids=[*protected_task_ids, *[task.id for task in tasks]],
    )
    runtime_store(root).save_runtime_transaction(
        task_intents={task.id: task.to_intent_record() for task in tasks},
        task_states={task.id: task_state_for_storage(task) for task in tasks},
        workspace_state=merged_state,
        task_journal_messages=journal_messages,
        audit_entries=audit_entries,
    )
    ensure_runtime_ignored(root)


def persist_task_and_state_without_runner_guard(
    root: Path,
    task: TaskRecord,
    state: WorkspaceState,
    journal_message: str | None = None,
    protected_task_ids: list[str] | tuple[str, ...] = (),
    audit_entries: list[TaskAuditEntry] | None = None,
) -> None:
    """
    Single-task variant of ``persist_tasks_and_state_without_runner_guard``.

    The common case on the runner's hot path where one task transitions
    per write; the multi-task variant is reserved for repair flows that
    coalesce several tasks into one transaction.
    """
    if journal_message is not None:
        journal_messages: dict[str, str] | None = {task.id: journal_message}
    else:
        journal_messages = None
    persist_tasks_and_state_without_runner_guard(
        root,
        tasks=[task],
        state=state,
        journal_messages=journal_messages,
        protected_task_ids=protected_task_ids,
        audit_entries=audit_entries,
    )
