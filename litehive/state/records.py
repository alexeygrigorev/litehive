"""Task CRUD operations: create, list, get, save, and related helpers."""

import logging
import os
import builtins
from collections.abc import Sequence
from pathlib import Path

from litehive.config.workspace import render_workspace_gitignore
from litehive.git.ops import default_commit_message
from litehive.domain.common import PipelineMode, PipelineStatus, TaskStage, TaskStatus, utcnow
from litehive.domain.reports import FollowUpTaskSpec
from litehive.fs_cleanup import remove_tree_logged
from litehive.domain.task import (
    TaskCreationSource,
    TaskRecord,
    TaskStateRecord,
    WorkspaceState,
    canonicalize_task_terminal_state,
)
from litehive.state.store import RuntimeStore

from litehive.tasks.constants import VALID_TASK_PRIORITIES
from litehive.state.locking import WorkspaceMutationGuard, WorkspaceStateLock
from litehive.state.persist import (
    WorkspaceStateRepository,
    write_atomic_files_and_then,
)
from litehive.tasks.audit import (
    TaskAuditEntry,
    build_task_audit_entry,
    snapshot_task_audit_state,
)
from litehive.tasks.normalization import normalize_acceptance_criteria
from litehive.tasks.paths import slugify
from litehive.workspace import Workspace

logger = logging.getLogger(__name__)

_MANUAL_CREATION_RATIONALE = "Created outside a Litehive agent session."


class TaskStateMissingError(RuntimeError):
    """
    Raised when a task has no SQLite runtime state row.

    Distinct from "task not found" so callers can choose to recover (the
    intent row is present, just the runtime side is missing) instead of
    treating the task as gone — losing only the runtime row is the
    common shape after a partial migration.
    """


class WorkspaceTasks:
    """
    Workspace-bound task record and runtime persistence service.

    This is the object-shaped owner for task CRUD behavior.
    """

    def __init__(self, workspace: Workspace, runtime_store: RuntimeStore | None = None) -> None:
        self.workspace = workspace
        self.runtime_store = runtime_store or RuntimeStore(workspace)

    def ensure_runtime_ignored(self) -> None:
        _ensure_runtime_ignored_for_workspace_impl(self.workspace)

    def write_runtime(self, task: TaskRecord) -> None:
        _write_task_runtime_for_workspace_impl(self.workspace, task)

    def save_runtime(self, task: TaskRecord) -> None:
        _save_task_runtime_for_workspace_impl(self.workspace, task)

    def load_runtime(self, task: TaskRecord) -> TaskRecord:
        return _load_task_runtime_impl(self.workspace, task)

    def create(
        self,
        title: str,
        depends_on: list[str] | None = None,
        pipeline_mode: str = "full",
        model: str | None = None,
        retry_limit: int | None = None,
        goal: str = "",
        acceptance_criteria: list[str] | None = None,
        auto_commit: bool = True,
        priority: str | None = None,
    ) -> TaskRecord:
        return _create_task_for_workspace_impl(
            self.workspace,
            title=title,
            depends_on=depends_on,
            pipeline_mode=pipeline_mode,
            model=model,
            retry_limit=retry_limit,
            goal=goal,
            acceptance_criteria=acceptance_criteria,
            auto_commit=auto_commit,
            priority=priority,
        )

    def next_task_id(self, state: WorkspaceState) -> str:
        """
        Reserve and return the next task id using the existing counter semantics.
        """
        return f"T-{_reserve_next_task_numbers_impl(self.workspace, state)[0]:04d}"

    def discard_created(self, task_id: str) -> None:
        _discard_created_task_for_workspace_impl(self.workspace, task_id)

    def list(self, include_runtime: bool = True, strict: bool = True) -> list[TaskRecord]:
        return _list_tasks_for_workspace_impl(self.workspace, include_runtime=include_runtime, strict=strict)

    def list_state_first(
        self,
        state: WorkspaceState | None = None,
        include_runtime: bool = False,
    ) -> builtins.list[TaskRecord]:
        return _list_tasks_state_first_impl(self.workspace, state=state, include_runtime=include_runtime)

    def create_follow_ups(
        self,
        parent_task: TaskRecord,
        stage: str,
        follow_ups: Sequence[FollowUpTaskSpec],
    ) -> Sequence[TaskRecord]:
        return _create_follow_up_tasks_impl(self.workspace, parent_task, stage, list(follow_ups))

    def get(self, task_id: str) -> TaskRecord | None:
        return _get_task_for_workspace_impl(self.workspace, task_id)

    def get_record(self, task_id: str) -> TaskRecord | None:
        return _get_task_record_for_workspace_impl(self.workspace, task_id)

    def require(self, task_id: str) -> TaskRecord:
        return _require_task_for_workspace_impl(self.workspace, task_id)

    def save(self, task: TaskRecord) -> None:
        _save_task_for_workspace_impl(self.workspace, task)

def _highest_task_number_in_store_impl(workspace: Workspace) -> int:
    """
    Return the largest ``T-NNNN`` numeric prefix actually present in the store.

    ``_reserve_next_task_numbers_impl`` consults this whenever the in-memory
    ``next_task_number`` is missing or zero so a freshly bootstrapped
    workspace cannot reuse an existing id when the counter was lost.
    """
    return RuntimeStore(workspace).highest_task_number()


def _reserve_next_task_numbers_impl(
    workspace: Workspace,
    state: WorkspaceState,
    count: int = 1,
) -> list[int]:
    """
    Allocate the next ``count`` task numbers and advance the workspace counter.

    Reserving in advance prevents the manual and follow-up creation
    paths from colliding on the same id when both run inside the same
    transaction (e.g. follow-up emission while the operator is also
    creating a sibling).
    """
    if count < 1:
        raise ValueError("count must be 1 or greater")
    if state.next_task_number <= 0:
        state.next_task_number = _highest_task_number_in_store_impl(workspace)
    start = state.next_task_number + 1
    state.next_task_number += count
    return list(range(start, start + count))


def _task_creation_stage_impl(workspace: Workspace, current_task_id: str | None) -> str | None:
    """
    Resolve the stage of the agent currently creating a sibling task.

    Prefers ``LITEHIVE_STAGE`` from the subagent environment, falls back
    to the parent task's runtime stage, then its pipeline status;
    ``_default_task_creation_source`` records this as provenance so
    follow-up audits can answer "which stage spawned this task?".
    """
    env_stage = (os.environ.get("LITEHIVE_STAGE") or "").strip()
    if env_stage:
        return env_stage
    if not current_task_id:
        return None
    current_task = _get_task_record_for_workspace_impl(workspace, current_task_id)
    if current_task is None:
        return None
    runtime_stage = current_task.current_pipeline_stage
    if runtime_stage:
        return runtime_stage
    pipeline_stage = current_task.pipeline_status
    if pipeline_stage and pipeline_stage != PipelineStatus.BACKLOG:
        return pipeline_stage
    return None


def _default_task_creation_source_impl(workspace: Workspace) -> TaskCreationSource:
    """
    Build the provenance attached to a new task at create time.

    Detects whether an agent is calling (via ``LITEHIVE_AGENT_ROLE``)
    and stamps the parent task/stage/role so audit logs can later answer
    "where did this task come from?" for any agent-spawned task; manual
    operator creation falls through to a generic ``manual`` source.
    """
    agent_role = (os.environ.get("LITEHIVE_AGENT_ROLE") or "").strip()
    current_task_id = (os.environ.get("LITEHIVE_TASK_ID") or "").strip() or None
    if not agent_role:
        return TaskCreationSource(
            source="manual",
            rationale=_MANUAL_CREATION_RATIONALE,
        )
    rationale = f"Created by Litehive agent role {agent_role}."
    if current_task_id:
        rationale = f"{rationale[:-1]} while working on {current_task_id}."
    return TaskCreationSource(
        source="agent",
        task_id=current_task_id,
        stage=_task_creation_stage_impl(workspace, current_task_id=current_task_id),
        role=agent_role,
        rationale=rationale,
    )


def _ensure_runtime_ignored_for_workspace_impl(workspace: Workspace) -> None:
    """
    Refresh the workspace ``.gitignore`` after any persistence write.

    Newly materialized runtime files (lockfiles, run logs, transcripts)
    must not be committed to the user's repo by accident; refreshing on
    every write keeps the ignore rules in sync with whatever the latest
    layout produces.
    """
    ignore_path = workspace.control_files().gitignore()
    expected = render_workspace_gitignore()
    if not ignore_path.exists() or ignore_path.read_text(encoding="utf-8") != expected:
        ignore_path.write_text(expected, encoding="utf-8")


def task_state_for_storage(task: TaskRecord) -> TaskStateRecord:
    """
    Canonicalise a task's mutable runtime fields and project to storage shape.

    Reconciles commit sha, worktree path, and flag reason before
    persistence; every write path (locking, persist, records) routes
    through this helper so the SQLite row reflects one agreed state
    rather than each caller picking its own normalisation.
    """
    _normalize_task_commit_sha_state(task)
    _normalize_task_worktree_state(task)
    _normalize_task_flag_reason(task)
    return task.to_storage_state_record()


def _write_task_runtime_for_workspace_impl(workspace: Workspace, task: TaskRecord) -> None:
    """
    Persist a task's runtime row without entering the workspace mutation guard.

    Used by engine adapters that already hold their own lock and need a
    raw save; entering the guard here would force re-entry on a thread
    that has already taken it via a different pathway.
    """
    RuntimeStore(workspace).save_task_state(task.id, task_state_for_storage(task))
    _ensure_runtime_ignored_for_workspace_impl(workspace)


def set_task_commit_sha(task: TaskRecord, commit_sha: str | None) -> None:
    """
    Mirror a commit sha onto both the intent and runtime sides of a task.

    The orchestration layer calls this after the commit stage produces a
    HEAD sha and again after the queue resets a task for retry; keeping
    both sides in sync means status surfaces and merge checks see one
    truth regardless of which slot they read.
    """
    task.git.commit_sha = commit_sha
    task.runtime.pipeline.git.commit_sha = commit_sha


def get_task_worktree_path(task: TaskRecord) -> str | None:
    """
    Return the worktree path the task is currently bound to.

    Prefers the runtime side (the new source of truth) and falls back to
    the legacy intent slot so worktree, recovery, and inspection callers
    see a single value during the runtime/intent migration.
    """
    return task.runtime.pipeline.git.worktree_path or task.git.worktree_path


def set_task_worktree_path(task: TaskRecord, worktree_path: str | None) -> None:
    """
    Record a worktree binding on the runtime side and clear the legacy slot.

    Clearing the intent slot is what stops the worktree service, rescue,
    and cleanup paths from later seeing a ghost path that disagrees with
    the runtime-side binding.
    """
    task.runtime.pipeline.git.worktree_path = worktree_path
    task.git.worktree_path = None


def clear_task_worktree_path(task: TaskRecord) -> None:
    """
    Detach a task from any recorded worktree.

    Called by worktree cleanup, rescue, and service flows once the
    on-disk worktree has been removed or merged; without the clear, the
    next selection could hand the runner a path that no longer exists.
    """
    set_task_worktree_path(task, None)


def _normalize_task_worktree_state(task: TaskRecord) -> None:
    """
    Reconcile runtime and intent worktree slots on persist and on load.

    Legacy rows still carry the path on the intent side; this helper
    migrates the binding onto the runtime side without losing it so a
    re-saved row never has the path on the wrong slot.
    """
    if task.runtime.pipeline.git.worktree_path:
        task.git.worktree_path = None
        return
    if task.git.worktree_path:
        set_task_worktree_path(task, task.git.worktree_path)


def _normalize_task_commit_sha_state(task: TaskRecord) -> None:
    """
    Keep intent and runtime commit-sha slots in sync.

    Run before persistence and after load so older rows that recorded
    the sha on only one side don't appear divergent to status snapshots
    and merge checks; without the normaliser the two slots could carry
    different values for the same commit.
    """
    if task.git.commit_sha:
        task.runtime.pipeline.git.commit_sha = task.git.commit_sha
        return
    if task.runtime.pipeline.git.commit_sha:
        task.git.commit_sha = task.runtime.pipeline.git.commit_sha


def _normalize_task_flag_reason(task: TaskRecord) -> None:
    """
    Reconcile terminal state and ensure ``flag_reason`` is set when flagged.

    Defaults to the last outcome's ``reason_code``, then to ``"unknown"``;
    ``task_state_for_storage`` calls this so the persisted row never shows
    ``status=flagged`` with an empty reason — operator status would
    otherwise render a flagged task without a "why".
    """
    canonicalize_task_terminal_state(task)
    if task.status == TaskStatus.FLAGGED:
        task.flag_reason = task.flag_reason or task.runtime.pipeline.last_outcome.reason_code or "unknown"
        return
    task.flag_reason = None


def _created_from_payload(task: TaskRecord) -> dict | None:
    """
    Return the audit-context shape of ``task.created_from``.

    ``None`` when the task was created from scratch; isolated so the
    create-task and follow-up audit paths share one null-handling rule
    instead of each branch dumping the model differently.
    """
    if task.created_from is None:
        return None
    return task.created_from.model_dump(mode="json")


def _create_task_runtime_dirs(base: Path) -> None:
    """
    Materialise the per-task runtime layout (reports, subagents, artifacts).

    Used by both the manual create path and the follow-up batch path;
    raises when directories already exist so a fresh task never silently
    reuses stale debris from a previous run that happened to share the
    slug.
    """
    (base / "reports").mkdir(parents=True, exist_ok=False)
    (base / "subagents").mkdir(parents=True, exist_ok=False)
    (base / "artifacts").mkdir(parents=True, exist_ok=False)


def _cleanup_created_task_dirs(paths: list[Path]) -> list[OSError]:
    """
    Best-effort rollback of on-disk task directories.

    Called when ``_persist_created_tasks`` fails after the directories
    have already landed; collects (rather than raises) cleanup errors
    so the caller can surface both the original DB failure and the
    cleanup damage in a single ``ExceptionGroup``.
    """
    errors: list[OSError] = []
    for path in reversed(paths):
        try:
            remove_tree_logged(path, logger=logger, target_label="created task directory")
        except OSError as cleanup_err:
            errors.append(cleanup_err)
    return errors


def _persist_created_tasks_impl(
    workspace: Workspace,
    *,
    tasks: list[TaskRecord],
    state: WorkspaceState,
    task_journal_messages: dict[str, str] | None = None,
    cleanup_dirs: list[Path],
    audit_entries: list[TaskAuditEntry] | None = None,
) -> None:
    """
    Atomically commit a batch of newly minted tasks.

    Writes intent rows, state rows, journal entries, audit entries, and
    workspace state in one transaction; rolls back the on-disk runtime
    directories if the SQLite write fails so a half-created task never
    leaves disk debris pointing at a row that was never committed.
    Shared by the manual create and follow-up creation paths.
    """
    # inline: kept so tests can monkey-patch the repository merge method and
    # have callers here see it.
    from litehive.state.persist import skip_bootstrap_load_state  # noqa: PLC0415

    with skip_bootstrap_load_state():
        merged_state = WorkspaceStateRepository(workspace).merged_state_for_runner_owned_write(
            state=state, protected_task_ids=[task.id for task in tasks]
        )
    try:

        def callback() -> None:
            """
            Single-shot SQLite write for the ``write_atomic_files_and_then`` flow.

            Pulling the ``runtime_store`` call into a closure lets the
            file-write step finish first so the DB transaction (which
            cannot be undone) only fires once the on-disk artifacts are
            safely in place.
            """
            RuntimeStore(workspace).save_runtime_transaction(
                task_intents={task.id: task.to_intent_record() for task in tasks},
                task_states={task.id: task_state_for_storage(task) for task in tasks},
                workspace_state=merged_state,
                task_journal_messages=task_journal_messages,
                audit_entries=audit_entries,
            )

        write_atomic_files_and_then({}, callback)
    except Exception as exc:
        cleanup_errors = _cleanup_created_task_dirs(cleanup_dirs)
        if cleanup_errors:
            raise ExceptionGroup(
                "failed to persist created tasks and roll back created task directories",
                [exc, *cleanup_errors],
            ) from exc
        raise


def _save_task_runtime_for_workspace_impl(workspace: Workspace, task: TaskRecord) -> None:
    """
    Persist a task's runtime row under the workspace mutation guard.

    Called by the lifecycle's runtime-update helpers when they need a
    fresh state snapshot without any accompanying journal or queue
    change; the guard makes the snapshot safe to take while a runner is
    also touching workspace state on a different transition.
    """
    with WorkspaceMutationGuard(workspace).hold():
        _write_task_runtime_for_workspace_impl(workspace, task)


def _load_task_runtime_impl(workspace: Workspace, task: TaskRecord) -> TaskRecord:
    """
    Hydrate a task's runtime row from SQLite and run the normalisers.

    ``get_task`` invokes this on the strict path that requires a runtime
    row; ``get_task_record`` calls it on the tolerant path that accepts
    the ``TaskStateMissingError`` so diagnostics can still report on a
    half-deleted task.
    """
    store = RuntimeStore(workspace)
    task_state = store.load_task_state(task.id)
    if task_state is None:
        raise TaskStateMissingError(f"Task {task.id} is missing its SQLite runtime state row")
    task = TaskRecord.from_intent_and_state(task.to_intent_record(), task_state)
    _normalize_task_commit_sha_state(task)
    _normalize_task_worktree_state(task)
    return task


def _create_task_for_workspace_impl(
    workspace: Workspace,
    title: str,
    depends_on: list[str] | None = None,
    pipeline_mode: str = "full",
    model: str | None = None,
    retry_limit: int | None = None,
    goal: str = "",
    acceptance_criteria: list[str] | None = None,
    auto_commit: bool = True,
    priority: str | None = None,
) -> TaskRecord:
    """
    Create and persist a single new task.

    The user-facing ``litehive task add`` CLI and the agent-facing
    task-creation tool both end here, so dependency validation,
    priority validation, queue insertion, and audit emission all live
    in one place rather than being duplicated across entry points.
    """
    workspace.create()
    if retry_limit is not None and retry_limit < 0:
        raise ValueError("Retry limit must be 0 or greater")
    try:
        pipeline_mode_enum = PipelineMode(pipeline_mode)
    except ValueError:
        raise ValueError(f"Unsupported pipeline_mode '{pipeline_mode}'") from None
    if priority is not None and priority not in VALID_TASK_PRIORITIES:
        raise ValueError(f"Unsupported priority '{priority}'; choose from {sorted(VALID_TASK_PRIORITIES)}")
    # inline: queue eligibility imports state.records for repository access.
    from litehive.tasks.queue_eligibility import TaskDependencyValidator  # noqa: PLC0415

    with WorkspaceStateLock(workspace).hold():
        state = WorkspaceStateRepository(workspace).load(bootstrap=False)
        task_id = f"T-{_reserve_next_task_numbers_impl(workspace, state)[0]:04d}"
        slug = slugify(title)
        if depends_on:
            TaskDependencyValidator(workspace).validate(task_id=task_id, depends_on=depends_on)
        task = TaskRecord(
            id=task_id,
            slug=slug,
            title=title,
            depends_on=list(depends_on or []),
            model=model,
            pipeline_mode=pipeline_mode_enum,
            priority=priority or "medium",
            goal=goal,
            acceptance_criteria=normalize_acceptance_criteria(acceptance_criteria),
            retry_policy={"max_retries": retry_limit},
            created_from=_default_task_creation_source_impl(workspace),
            git={
                "auto_commit": auto_commit,
                "commit_message": default_commit_message(task_id, slug),
            },
        )

        base = workspace.task_dir(task, bootstrap=False)
        _create_task_runtime_dirs(base)
        state.queue.append(task.id)
        actor = "operator"
        source = "manual"
        if task.created_from is not None and task.created_from.source == "agent":
            actor = "agent"
            source = "agent"
        elif task.created_from is not None and task.created_from.source == "follow_up":
            actor = "system"
            source = "follow_up"
        if task.created_from is None:
            created_from_payload = None
        else:
            created_from_payload = task.created_from.model_dump(mode="json")
        _persist_created_tasks_impl(
            workspace,
            tasks=[task],
            state=state,
            task_journal_messages={task.id: "Task created."},
            cleanup_dirs=[base],
            audit_entries=[
                build_task_audit_entry(
                    task_id=task.id,
                    action="created",
                    actor=actor,
                    source=source,
                    after_task=task,
                    after_queue=state.queue,
                    context={
                        "title": task.title,
                        "priority": task.priority,
                        "pipeline_mode": str(task.pipeline_mode),
                        "created_from": created_from_payload,
                    },
                )
            ],
        )
        _ensure_runtime_ignored_for_workspace_impl(workspace)
        return task


def _follow_up_journal_messages(
    created_tasks: list[TaskRecord],
    follow_ups: list[FollowUpTaskSpec],
    parent_task: TaskRecord,
    stage: str,
) -> dict[str, str]:
    """
    Build the per-follow-up "Task created" journal entries.

    Each entry records the parent task and originating stage so
    the journal of the new task explains its provenance even if
    the parent record is later deleted. Caller:
    :meth:`WorkspaceTasks.create_follow_ups`.
    """
    messages: dict[str, str] = {}
    for task, follow_up in zip(created_tasks, follow_ups):
        messages[task.id] = (
            "Task created.\n\n"
            f"Created as a follow-up from `{parent_task.id}` during `{stage}`.\n"
            f"Rationale: {follow_up.rationale}"
        )
    return messages


def _follow_up_audit_entries(
    created_tasks: list[TaskRecord],
    queue_after: list[str],
) -> list:
    """
    Build the ``"created"`` audit entry for each new follow-up task.

    Threads the post-creation queue snapshot so each entry records
    the queue ordering at the moment the follow-up was created;
    consistent with how :meth:`WorkspaceTasks.create` audits a single task.
    Caller: :meth:`WorkspaceTasks.create_follow_ups`.
    """
    entries: list = []
    for task in created_tasks:
        entries.append(
            build_task_audit_entry(
                task_id=task.id,
                action="created",
                actor="system",
                source="follow_up",
                after_task=task,
                after_queue=queue_after,
                context={
                    "title": task.title,
                    "priority": task.priority,
                    "pipeline_mode": str(task.pipeline_mode),
                    "created_from": _created_from_payload(task),
                },
            )
        )
    return entries


def _create_follow_up_tasks_impl(
    workspace: Workspace,
    parent_task: TaskRecord,
    stage: str,
    follow_ups: list[FollowUpTaskSpec],
) -> list[TaskRecord]:
    """
    Spawn follow-up tasks emitted by grooming/testing/accepting reports.

    Persists the batch atomically so a partial failure cannot leave
    half-created sibling tasks behind; only fires for stages that may
    legitimately emit follow-ups, ignoring follow-up blocks from
    pipeline stages where they are meaningless.
    """
    if not follow_ups:
        return []
    if stage not in {TaskStage.GROOMING, TaskStage.TESTING, TaskStage.ACCEPTING}:
        return []

    workspace.create()
    created_tasks: list[TaskRecord] = []
    created_dirs: list[Path] = []
    with WorkspaceMutationGuard(workspace).hold(), WorkspaceStateLock(workspace).hold():
        state = WorkspaceStateRepository(workspace).load(bootstrap=False)
        reserved_numbers = _reserve_next_task_numbers_impl(workspace, state, count=len(follow_ups))

        for next_number, follow_up in zip(reserved_numbers, follow_ups):
            task_id = f"T-{next_number:04d}"
            slug = slugify(follow_up.title)
            task = TaskRecord(
                id=task_id,
                slug=slug,
                title=follow_up.title,
                goal=follow_up.goal,
                acceptance_criteria=normalize_acceptance_criteria(follow_up.acceptance_criteria),
                created_from=TaskCreationSource(
                    source="follow_up",
                    task_id=parent_task.id,
                    stage=stage,
                    rationale=follow_up.rationale,
                    blocking=follow_up.blocking,
                ),
                git={
                    "auto_commit": True,
                    "commit_message": default_commit_message(task_id, slug),
                },
            )

            base = workspace.task_dir(task, bootstrap=False)
            _create_task_runtime_dirs(base)
            created_dirs.append(base)
            state.queue.append(task.id)
            created_tasks.append(task)

        journal_messages = _follow_up_journal_messages(
            created_tasks=created_tasks,
            follow_ups=follow_ups,
            parent_task=parent_task,
            stage=stage,
        )
        audit_entries = _follow_up_audit_entries(created_tasks, state.queue)
        _persist_created_tasks_impl(
            workspace,
            tasks=created_tasks,
            state=state,
            task_journal_messages=journal_messages,
            cleanup_dirs=created_dirs,
            audit_entries=audit_entries,
        )
        _ensure_runtime_ignored_for_workspace_impl(workspace)
    return created_tasks


def _discard_created_task_for_workspace_impl(workspace: Workspace, task_id: str) -> None:
    """
    Remove a task that should never have existed.

    Drops the queue entry, clears it from ``active_task_id`` if set,
    deletes the on-disk directory, and tombstones the SQLite rows;
    called by the rollback path when a creation step fails downstream
    of the initial persist so the workspace is left in the
    pre-creation shape.
    """
    with WorkspaceStateLock(workspace).hold():
        task = _get_task_for_workspace_impl(workspace, task_id)
        state = WorkspaceStateRepository(workspace).load()
        queue_before = list(state.queue)
        before_task = snapshot_task_audit_state(task)
        if state.active_task_id == task_id:
            state.active_task_id = None
        state.queue = [queued_id for queued_id in state.queue if queued_id != task_id]
        WorkspaceStateRepository(workspace).save_without_runner_guard(state)
        if task is not None:
            td = workspace.task_dir(task)
            if td.exists():
                remove_tree_logged(td, logger=logger, target_label="task directory")
        RuntimeStore(workspace).delete_task_records(
            task_id,
            audit_entries=[
                build_task_audit_entry(
                    task_id=task_id,
                    action="removed",
                    actor="system",
                    source="task_cleanup",
                    before_task=before_task,
                    after_task=None,
                    before_queue=queue_before,
                    after_queue=state.queue,
                    context={"task_missing": task is None},
                )
            ],
        )


def _load_tasks_from_store_impl(
    workspace: Workspace,
    include_runtime: bool,
    strict: bool,
) -> list[TaskRecord]:
    """
    Iterate every stored task intent and pair it with its runtime row.

    The two listing entry points (`WorkspaceTasks.list` and
    `WorkspaceTasks.list_state_first`) share this helper so they apply the
    same strict/lenient policy when a runtime row is missing or
    malformed; without the shared core, the two would drift in their
    handling of half-deleted tasks.
    """
    store = RuntimeStore(workspace)
    records: list[TaskRecord] = []
    for intent in store.list_task_intents():
        try:
            state_record = store.load_task_state(intent.id)
            stateful_task = TaskRecord.from_intent_and_state(intent, state_record)
            if include_runtime:
                if state_record is None:
                    raise TaskStateMissingError(f"Task {intent.id} is missing its SQLite runtime state row")
                task = stateful_task
            else:
                task = TaskRecord.from_intent_and_state(intent)
        except (TaskStateMissingError, ValueError):
            if strict:
                raise
            continue
        records.append(task)
    return records


def _list_tasks_for_workspace_impl(
    workspace: Workspace,
    include_runtime: bool = True,
    strict: bool = True,
) -> list[TaskRecord]:
    """
    Return every task in id order with its runtime row attached.

    Used by the queue selector, status snapshot builder, recovery
    probes, and worktree inspection when they need the full task
    population; ``strict=False`` lets recovery still see tasks whose
    runtime row was deleted out from under them.
    """
    return _load_tasks_from_store_impl(
        workspace,
        include_runtime=include_runtime,
        strict=strict,
    )


def _list_tasks_state_first_impl(
    workspace: Workspace,
    state: WorkspaceState | None = None,
    include_runtime: bool = False,
) -> list[TaskRecord]:
    """
    Return tasks ordered by workspace priority.

    Active task first, then queued tasks in queue order, then everything
    else by id; status displays and the operator CLI render this
    ordering directly so the user sees the work-in-flight at the top
    without having to re-sort client-side.
    """
    task_by_id = {
        task.id: task
        for task in _load_tasks_from_store_impl(workspace, include_runtime=include_runtime, strict=True)
    }

    if state is None:
        workspace_state = WorkspaceStateRepository(workspace).load()
    else:
        workspace_state = state
    ordered_ids: list[str] = []
    seen: set[str] = set()

    def add(task_id: str | None) -> None:
        """
        Append a task id to the running ordering at most once.

        Skips unknown or duplicate ids so the three append passes
        (active, queued, id-sorted leftovers) can be applied in
        priority order without each pass re-checking for duplicates.
        """
        if task_id is None or task_id in seen or task_id not in task_by_id:
            return
        seen.add(task_id)
        ordered_ids.append(task_id)

    add(workspace_state.active_task_id)
    for task_id in workspace_state.queue:
        add(task_id)
    for task_id in sorted(task_by_id):
        add(task_id)

    return [task_by_id[task_id] for task_id in ordered_ids]


def _get_task_for_workspace_impl(workspace: Workspace, task_id: str) -> TaskRecord | None:
    """
    Look up a task by id and require its runtime row to exist.

    The orchestration loop, queue selector, and audit emitters use
    this when they cannot continue without a fully hydrated task and
    would rather raise on a half-deleted row than guess at runtime
    state from intent-only data.
    """
    intent = RuntimeStore(workspace).load_task_intent(task_id)
    if intent is None:
        return None
    task = _load_task_runtime_impl(workspace, TaskRecord.from_intent_and_state(intent))
    return task


def _get_task_record_for_workspace_impl(workspace: Workspace, task_id: str) -> TaskRecord | None:
    """
    Return the task record, tolerating a missing runtime row.

    Recovery and diagnostics flows use this so they can still report on
    a task whose runtime row was deleted out from under them — the
    task's intent is recoverable, but forcing a runtime row to exist
    would mask exactly the corruption these flows want to surface.
    """
    intent = RuntimeStore(workspace).load_task_intent(task_id)
    if intent is None:
        return None
    task = TaskRecord.from_intent_and_state(intent)
    try:
        task = _load_task_runtime_impl(workspace, task)
    except TaskStateMissingError:
        pass
    return task


def _require_task_for_workspace_impl(workspace: Workspace, task_id: str) -> TaskRecord:
    """
    Look up a task by id and raise if it does not exist.

    CLI handlers and engine adapters call this when a missing task is
    a programmer/operator error rather than an expected absence; the
    raise stops them from silently continuing on a ``None`` they would
    have to dereference downstream anyway.
    """
    task = _get_task_for_workspace_impl(workspace, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    return task


def _save_task_for_workspace_impl(workspace: Workspace, task: TaskRecord) -> None:
    """
    Persist both the intent and runtime sides of a task atomically.

    Refreshes ``updated_at`` and runs under the workspace mutation
    guard; stage transition helpers and CLI mutators (status edits,
    retry resets) call this when they need a single-task write
    without touching workspace-level queue state.
    """
    task.updated_at = utcnow()
    with WorkspaceMutationGuard(workspace).hold():
        write_atomic_files_and_then(
            {},
            lambda: RuntimeStore(workspace).save_runtime_transaction(
                task_intents={task.id: task.to_intent_record()},
                task_states={task.id: task_state_for_storage(task)},
            ),
        )
        _ensure_runtime_ignored_for_workspace_impl(workspace)
