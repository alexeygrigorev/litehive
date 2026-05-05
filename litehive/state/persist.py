"""Workspace state persistence and atomic write helpers."""

import gzip
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.domain.common import utcnow
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
    """Suppress workspace bootstrap inside `load_state`; entered by recovery and inspection paths that must read state without provisioning workspace files on disk."""
    token = _SKIP_BOOTSTRAP_LOAD_STATE.set(True)
    try:
        yield
    finally:
        _SKIP_BOOTSTRAP_LOAD_STATE.reset(token)


def load_state(root: Path, bootstrap: bool = True) -> WorkspaceState:
    """Return the workspace state, materialising an empty state row on first read; the canonical reader used everywhere the runner, CLI, or dashboards need queue/runner pointers."""
    if bootstrap and not _SKIP_BOOTSTRAP_LOAD_STATE.get():
        ensure_workspace(root)
    store = runtime_store(root)
    state = store.load_workspace_state()
    if state is None:
        state = WorkspaceState()
        store.save_workspace_state(state)
    return state


def atomic_write_text(path: Path, content: str) -> None:
    """Write a file via tmp+rename with fsync so partial writes are never visible to readers; fsync is suppressed under LITEHIVE_SKIP_FSYNC to keep the test suite fast."""
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
    """Atomic-write variant for gzipped artifacts (subagent transcripts, prompt dumps); keeps tmp+rename semantics so half-written .gz files never appear on disk."""
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
    """Return the current content of `path`, or the `_MISSING` sentinel when the file does not exist; used to capture a rollback baseline before a batch write."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return _MISSING


def write_atomic_files(writes: dict[Path, str]) -> None:
    """Write a batch of files all-or-nothing: if any write fails, rolls back already-applied writes from in-memory snapshots so a partial multi-file update is never observable."""
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
            atomic_write_text(path, previous)
        raise


def write_atomic_files_and_then(writes: dict[Path, str], callback) -> None:
    """Same all-or-nothing batch write as `write_atomic_files`, but treats `callback` as part of the transaction — if the callback raises, the file writes are rolled back too. Used when a downstream side effect (e.g. SQLite commit) must be atomic with the file writes."""
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
            atomic_write_text(path, previous)
        raise


def save_state(root: Path, state: WorkspaceState) -> None:
    """Persist workspace state under the mutation guard; the path used by CLI commands that change queue/pool settings outside an active runner."""
    with workspace_mutation_guard(root):
        runtime_store(root).save_workspace_state(state)


def save_state_without_runner_guard(
    root: Path,
    state: WorkspaceState,
    audit_entries: list[TaskAuditEntry] | None = None,
) -> None:
    """Persist workspace state assuming the caller already holds the runner guard (or is running inside it); used on the runner's hot path so nested mutations don't reacquire the same lock."""
    if audit_entries:
        runtime_store(root).save_runtime_transaction(
            workspace_state=state,
            audit_entries=audit_entries,
        )
        return
    runtime_store(root).save_workspace_state(state)


def record_task_completion(root: Path, final_stage: str | None) -> tuple[int, str | None]:
    """Update the consecutive-failure counter and trigger pool stop when the limit is hit; called by the runner after each task finishes so a streak of failures halts the pool instead of grinding through every task."""
    with workspace_lock(root):
        state = load_state(root)
        if final_stage == "done":
            state.consecutive_task_failures = 0
        else:
            state.consecutive_task_failures = max(0, int(state.consecutive_task_failures)) + 1
            if state.consecutive_task_failures >= CONSECUTIVE_TASK_FAILURE_LIMIT:
                state.pool_stop_reason = CONSECUTIVE_TASK_FAILURE_STOP_REASON
        save_state_without_runner_guard(root, state)
        return state.consecutive_task_failures, state.pool_stop_reason


def set_pool_stop_reason(root: Path, stop_reason: str | None) -> WorkspaceState:
    """Set or clear the pool's stop reason; clearing the consecutive-failure stop also resets the counter so operators don't have to chase two fields when resuming the pool."""
    with workspace_lock(root):
        state = load_state(root)
        if stop_reason is None and state.pool_stop_reason == CONSECUTIVE_TASK_FAILURE_STOP_REASON:
            state.consecutive_task_failures = 0
        state.pool_stop_reason = stop_reason
        save_state_without_runner_guard(root, state)
        return state


def workspace_transition_writes(
    root: Path,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...] = (),
    state: WorkspaceState | None = None,
    journal_messages: dict[str, str] | None = None,
) -> dict[Path, str]:
    """Vestigial hook from the file-based era: returns no file writes now that SQLite owns transitions. Kept as the call-site shape so legacy callers continue to compose with the atomic-batch helpers without behaviour change."""
    del root, tasks, state, journal_messages
    return {}


def _merge_queue_preserving_future_changes(
    desired_queue: list[str],
    latest_queue: list[str],
    protected_task_ids: list[str] | tuple[str, ...],
) -> list[str]:
    """Splice protected task ids into the latest persisted queue at the positions the in-flight write picked.

    Used by the persist path when another writer (CLI ``queue add`` etc.)
    has reordered the queue between the time the runner read state and the
    time it tries to write back: the runner only protects ids it just
    promoted, so unrelated concurrent edits survive instead of being
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
    """Rebase the runner's in-memory workspace state onto whatever the persisted state looks like, preserving the runner's edits to protected tasks while picking up concurrent CLI changes (queue reorders, future-task additions). Prevents the runner from clobbering operator edits made while a task was in flight."""
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
    """Single-task convenience over `persist_tasks_and_state`; the common case used by stage transitions that mutate exactly one task plus the workspace queue."""
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
    """Atomic write of tasks plus workspace state under the mutation guard; the runner-owned write path that picks up concurrent operator edits via `merged_state_for_runner_owned_write` before persisting."""
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
    """Same atomic write as `persist_tasks_and_state` but assumes the caller already holds the runner guard; used inside the runner's hot loop where the guard is held for the whole task lifetime."""
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
    """Single-task convenience over `persist_tasks_and_state_without_runner_guard`; the common case on the runner's hot path where one task transitions per write."""
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
