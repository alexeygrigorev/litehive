"""Task storage helpers for the local YAML workspace."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import re
from contextlib import contextmanager
import os
from pathlib import Path
import sys

import yaml

from litehive.config import (
    VALID_POOL_SELECTION_POLICIES,
    ensure_workspace,
    load_config,
    state_path,
    workspace_dir,
)
from litehive.models import (
    RuntimeEngineSwitch,
    RuntimeSubagentState,
    StageReport,
    SubagentRef,
    TaskOutcomeState,
    TaskRecord,
    TaskRuntime,
    WorkspaceState,
    utcnow,
)

VALID_TASK_PRIORITIES = {"low", "medium", "high"}
VALID_TASK_ENGINES = {"codex", "opencode", "gemini", "copilot", "claude"}
TASK_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_RUNNER_LOCKS: dict[Path, tuple[object, int]] = {}


@dataclass(slots=True)
class BlockedTask:
    task_id: str
    title: str
    queue_position: int
    blocked_by: list[str]


@dataclass(slots=True)
class TaskSelection:
    task: TaskRecord | None
    blocked: list[BlockedTask]


class WorkspaceConflictError(ValueError):
    """Raised when workspace mutations would conflict with an active runner."""


def load_state(root: Path) -> WorkspaceState:
    ensure_workspace(root)
    data = yaml.safe_load(state_path(root).read_text(encoding="utf-8")) or {}
    return WorkspaceState(**data)


def save_state(root: Path, state: WorkspaceState) -> None:
    with workspace_mutation_guard(root):
        state_path(root).write_text(
            yaml.safe_dump(state.model_dump(mode="python"), sort_keys=False),
            encoding="utf-8",
        )


def set_pool_stop_reason(root: Path, stop_reason: str | None) -> WorkspaceState:
    with _workspace_lock(root):
        state = load_state(root)
        state.pool_stop_reason = stop_reason
        save_state(root, state)
        return state


def tasks_root(root: Path) -> Path:
    ensure_workspace(root)
    return workspace_dir(root) / "tasks"


def runner_lock_path(root: Path) -> Path:
    ensure_workspace(root)
    return workspace_dir(root) / ".runner.lock"


@contextmanager
def _workspace_lock(root: Path):
    lock_path = workspace_dir(root) / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_runner_lock_metadata(handle: object, root: Path) -> None:
    metadata = {
        "pid": os.getpid(),
        "workspace": str(root.resolve()),
        "started_at": utcnow(),
        "command": " ".join(sys.argv),
    }
    handle.seek(0)
    handle.truncate()
    handle.write(yaml.safe_dump(metadata, sort_keys=False))
    handle.flush()


def _read_runner_lock_metadata(root: Path) -> dict[str, object]:
    lock_path = runner_lock_path(root)
    if not lock_path.exists():
        return {}
    data = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _runner_conflict_message(root: Path) -> str:
    metadata = _read_runner_lock_metadata(root)
    pid = metadata.get("pid")
    started_at = metadata.get("started_at")
    command = metadata.get("command")
    details = []
    if pid is not None:
        details.append(f"pid={pid}")
    if started_at:
        details.append(f"started_at={started_at}")
    if command:
        details.append(f"command={command}")
    suffix = f" ({', '.join(details)})" if details else ""
    return (
        f"workspace is already being mutated by another runner{suffix}. "
        "Wait for the active run to finish before changing this workspace."
    )


@contextmanager
def workspace_runner_guard(root: Path):
    root = root.resolve()
    existing = _RUNNER_LOCKS.get(root)
    if existing is not None:
        handle, depth = existing
        _RUNNER_LOCKS[root] = (handle, depth + 1)
        try:
            yield
        finally:
            handle, depth = _RUNNER_LOCKS[root]
            if depth <= 1:
                _RUNNER_LOCKS.pop(root, None)
            else:
                _RUNNER_LOCKS[root] = (handle, depth - 1)
        return

    lock_path = runner_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceConflictError(_runner_conflict_message(root)) from exc
        _write_runner_lock_metadata(handle, root)
        _RUNNER_LOCKS[root] = (handle, 1)
        try:
            yield
        finally:
            _RUNNER_LOCKS.pop(root, None)
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def workspace_mutation_guard(root: Path):
    root = root.resolve()
    if root in _RUNNER_LOCKS:
        yield
        return
    with workspace_runner_guard(root):
        yield


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "task"


def _next_task_id(root: Path) -> str:
    existing = []
    for child in tasks_root(root).iterdir():
        if not child.is_dir():
            continue
        match = re.match(r"^T-(\d{4})-", child.name)
        if match:
            existing.append(int(match.group(1)))
    next_number = max(existing, default=0) + 1
    return f"T-{next_number:04d}"


def task_dir(root: Path, task: TaskRecord) -> Path:
    return tasks_root(root) / f"{task.id}-{task.slug}"


def task_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "task.yaml"


def task_runtime_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "runtime.yaml"


def _ensure_runtime_ignored(root: Path) -> None:
    git_info_exclude = root / ".git" / "info" / "exclude"
    if not git_info_exclude.exists():
        return
    existing = git_info_exclude.read_text(encoding="utf-8")
    entries = [
        ".litehive/.lock",
        ".litehive/.runner.lock",
        ".litehive/state.yaml",
        ".litehive/tasks/*/reports/commit_to_git-*.yaml",
        ".litehive/tasks/*/runtime.yaml",
    ]
    missing_entries = [entry for entry in entries if entry not in existing.splitlines()]
    if not missing_entries:
        return
    with git_info_exclude.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        for entry in missing_entries:
            handle.write(f"{entry}\n")


def _write_task_runtime(root: Path, task: TaskRecord) -> None:
    task_runtime_file(root, task).write_text(
        yaml.safe_dump(
            {
                **task.runtime.model_dump(mode="python"),
                "git": {"commit_sha": task.git.commit_sha},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _ensure_runtime_ignored(root)


def save_task_runtime(root: Path, task: TaskRecord) -> None:
    with workspace_mutation_guard(root):
        _write_task_runtime(root, task)


def _load_task_runtime(root: Path, task: TaskRecord) -> TaskRecord:
    runtime_file = task_runtime_file(root, task)
    if not runtime_file.exists():
        return task
    data = yaml.safe_load(runtime_file.read_text(encoding="utf-8")) or {}
    task.runtime = TaskRuntime(**data)
    task.git.commit_sha = task.runtime.git.commit_sha
    return task


def create_task(
    root: Path,
    *,
    title: str,
    depends_on: list[str] | None = None,
    mode: str = "implementation",
    engine: str | None = None,
    retry_limit: int | None = None,
    goal: str = "",
    acceptance_criteria: list[str] | None = None,
    auto_commit: bool = True,
) -> TaskRecord:
    ensure_workspace(root)
    if retry_limit is not None and retry_limit < 0:
        raise ValueError("Retry limit must be 0 or greater")
    with workspace_mutation_guard(root), _workspace_lock(root):
        task_id = _next_task_id(root)
        slug = slugify(title)
        _validate_task_dependencies(root, task_id=task_id, depends_on=depends_on or [])
        task = TaskRecord(
            id=task_id,
            slug=slug,
            title=title,
            depends_on=list(depends_on or []),
            engine=engine,
            mode=mode,  # type: ignore[arg-type]
            goal=goal,
            acceptance_criteria=acceptance_criteria or [],
            retry_policy={"max_retries": retry_limit},
            git={
                "auto_commit": auto_commit,
                "commit_message": f"litehive: checkpoint {task_id} {slug}",
            },
        )

        base = task_dir(root, task)
        (base / "reports").mkdir(parents=True, exist_ok=False)
        (base / "subagents").mkdir(parents=True, exist_ok=False)
        (base / "artifacts").mkdir(parents=True, exist_ok=False)
        task_file(root, task).write_text(
            yaml.safe_dump(task.model_dump(mode="python"), sort_keys=False),
            encoding="utf-8",
        )
        _write_task_runtime(root, task)
        (base / "journal.md").write_text(
            f"# {task.id} {task.title}\n\n## {utcnow()}\nTask created.\n",
            encoding="utf-8",
        )

        state = load_state(root)
        state.queue.append(task.id)
        save_state(root, state)
        return task


def list_tasks(root: Path) -> list[TaskRecord]:
    records: list[TaskRecord] = []
    for child in sorted(tasks_root(root).iterdir()):
        if not child.is_dir():
            continue
        path = child / "task.yaml"
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        records.append(_load_task_runtime(root, TaskRecord(**data)))
    return records


def get_task(root: Path, task_id: str) -> TaskRecord | None:
    for task in list_tasks(root):
        if task.id == task_id:
            return task
    return None


def require_task(root: Path, task_id: str) -> TaskRecord:
    task = get_task(root, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    return task


def save_task(root: Path, task: TaskRecord) -> None:
    task.updated_at = utcnow()
    task_payload = task.model_dump(mode="python")
    task_payload["git"]["commit_sha"] = None
    with workspace_mutation_guard(root):
        task_file(root, task).write_text(
            yaml.safe_dump(task_payload, sort_keys=False),
            encoding="utf-8",
        )
        _write_task_runtime(root, task)


def mark_task_run_started(root: Path, task: TaskRecord) -> None:
    now = utcnow()
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = now
    task.runtime.updated_at = now
    task.runtime.retry_count = 0
    task.runtime.retry_limit = task.runtime.retry_limit
    task.runtime.last_outcome = TaskOutcomeState()
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": None,
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    task.runtime.active_subagent = None
    save_task_runtime(root, task)


def mark_task_run_finished(root: Path, task: TaskRecord, final_status: str) -> None:
    now = utcnow()
    task.runtime.execution_status = final_status
    task.runtime.updated_at = now
    task.runtime.active_subagent = None
    save_task_runtime(root, task)


def set_task_retry_state(
    root: Path,
    task: TaskRecord,
    *,
    retry_count: int,
    retry_limit: int,
    retry_source: str,
) -> None:
    task.runtime.updated_at = utcnow()
    task.runtime.retry_count = retry_count
    task.runtime.retry_limit = retry_limit
    task.runtime.retry_source = retry_source
    save_task_runtime(root, task)


def clear_task_outcome(root: Path, task: TaskRecord) -> None:
    task.runtime.updated_at = utcnow()
    task.runtime.last_outcome = TaskOutcomeState()
    save_task_runtime(root, task)


def mark_task_outcome(
    root: Path,
    task: TaskRecord,
    *,
    kind: str,
    stage: str,
    reason_code: str,
    reason: str,
    retry_count: int,
    retry_limit: int,
    retry_source: str,
) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.last_outcome = TaskOutcomeState(
        kind=kind,
        stage=stage,
        reason_code=reason_code,
        reason=reason,
        retry_count=retry_count,
        retry_limit=retry_limit,
        retry_source=retry_source,
        recorded_at=now,
    )
    save_task_runtime(root, task)


def mark_stage_started(root: Path, task: TaskRecord, step: str) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": step,
            "status": "running",
            "started_at": now,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    save_task_runtime(root, task)


def mark_stage_finished(root: Path, task: TaskRecord, report: StageReport) -> None:
    now = utcnow()
    started_at = task.runtime.current_stage.started_at
    task.runtime.updated_at = now
    task.runtime.last_stage = task.runtime.last_stage.model_copy(
        update={
            "step": report.step,
            "status": "completed" if report.verdict in {"pass", "accept"} else report.verdict,
            "started_at": started_at,
            "completed_at": now,
            "updated_at": now,
            "duration_seconds": _duration_seconds(started_at, now),
            "verdict": report.verdict,
            "summary": report.summary,
        }
    )
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={
            "step": None,
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "duration_seconds": 0,
            "verdict": None,
            "summary": "",
        }
    )
    save_task_runtime(root, task)


def mark_subagent_started(root: Path, task: TaskRecord, ref: SubagentRef) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.active_subagent = RuntimeSubagentState(
        id=ref.id,
        role=ref.role,
        engine=ref.engine,
        status=ref.status,
        path=ref.path,
        started_at=now,
        updated_at=now,
    )
    save_task_runtime(root, task)


def mark_subagent_finished(
    root: Path,
    task: TaskRecord,
    ref: SubagentRef,
    transcript: str,
    exit_code: int,
) -> None:
    now = utcnow()
    started_at = task.runtime.active_subagent.started_at if task.runtime.active_subagent else now
    task.runtime.updated_at = now
    task.runtime.last_subagent = RuntimeSubagentState(
        id=ref.id,
        role=ref.role,
        engine=ref.engine,
        status=ref.status,
        path=ref.path,
        started_at=started_at,
        updated_at=now,
        completed_at=now,
        exit_code=exit_code,
        transcript_snippet=summarize_transcript(transcript),
    )
    task.runtime.active_subagent = None
    save_task_runtime(root, task)


def mark_engine_switch(
    root: Path,
    task: TaskRecord,
    *,
    step: str,
    from_engine: str,
    to_engine: str,
    reason: str,
) -> None:
    now = utcnow()
    task.runtime.updated_at = now
    task.runtime.last_engine_switch = RuntimeEngineSwitch(
        step=step,
        from_engine=from_engine,
        to_engine=to_engine,
        reason=reason,
        happened_at=now,
    )
    save_task_runtime(root, task)


def summarize_transcript(transcript: str, limit: int = 120) -> str:
    for line in transcript.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("VERDICT:"):
            continue
        if stripped.startswith("SUMMARY:"):
            stripped = stripped.partition(":")[2].strip()
        return stripped if len(stripped) <= limit else stripped[: limit - 3].rstrip() + "..."
    return ""


def _duration_seconds(started_at: str | None, ended_at: str | None) -> int:
    if started_at is None or ended_at is None:
        return 0
    try:
        from datetime import datetime

        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at)
    except ValueError:
        return 0
    return max(0, int((ended - started).total_seconds()))


def append_journal(root: Path, task: TaskRecord, message: str) -> None:
    journal = task_dir(root, task) / "journal.md"
    with workspace_mutation_guard(root):
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {utcnow()}\n{message}\n")


def set_active_task(root: Path, task_id: str | None) -> WorkspaceState:
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        state.active_task_id = task_id
        if task_id is not None and task_id in state.queue:
            state.queue = [item for item in state.queue if item != task_id]
        _validate_single_active_task(root, state)
        save_state(root, state)
        return state


def peek_next_task(root: Path) -> TaskRecord | None:
    return peek_next_task_selection(root).task


def peek_next_task_selection(root: Path) -> TaskSelection:
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        _validate_single_active_task(root, state)
        next_task, blocked, mutated = _resolve_next_task_from_state(root, state)
        if mutated:
            save_state(root, state)
        return TaskSelection(task=next_task, blocked=blocked)


def dequeue_next_task(root: Path) -> TaskRecord | None:
    return dequeue_next_task_selection(root).task


def dequeue_next_task_selection(root: Path) -> TaskSelection:
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        _validate_single_active_task(root, state)
        next_task, blocked, mutated = _resolve_next_task_from_state(root, state)
        if next_task is None:
            if mutated:
                save_state(root, state)
            return TaskSelection(task=None, blocked=blocked)
        if state.active_task_id != next_task.id:
            state.active_task_id = next_task.id
            state.queue = [item for item in state.queue if item != next_task.id]
            mutated = True
        if mutated:
            save_state(root, state)
        return TaskSelection(task=next_task, blocked=blocked)


def _is_task_eligible_for_execution(task: TaskRecord) -> bool:
    return task.status in {"queued", "in_progress"} and task.pipeline_status != "done"


def _is_task_completed(task: TaskRecord) -> bool:
    return task.status == "done" and task.pipeline_status == "done"


def _task_blockers(task: TaskRecord, tasks_by_id: dict[str, TaskRecord]) -> list[str]:
    blockers: list[str] = []
    seen: set[str] = set()
    for dependency_id in task.depends_on:
        if dependency_id in seen:
            continue
        seen.add(dependency_id)
        dependency = tasks_by_id.get(dependency_id)
        if dependency is None:
            blockers.append(f"{dependency_id} (missing)")
            continue
        if not _is_task_completed(dependency):
            blockers.append(f"{dependency.id} ({dependency.status}/{dependency.pipeline_status})")
    return blockers


def _validate_task_dependencies(root: Path, *, task_id: str, depends_on: list[str]) -> None:
    tasks_by_id = {task.id: task for task in list_tasks(root)}
    seen: set[str] = set()
    for dependency_id in depends_on:
        if dependency_id in seen:
            continue
        seen.add(dependency_id)
        if dependency_id == task_id:
            raise ValueError(f"Task {task_id} cannot depend on itself")
        if dependency_id not in tasks_by_id:
            raise ValueError(f"Task {dependency_id} not found")
        if _dependency_reaches_task(task_id, dependency_id, tasks_by_id):
            raise ValueError(f"Task {task_id} dependency cycle detected via {dependency_id}")


def _dependency_reaches_task(
    task_id: str, dependency_id: str, tasks_by_id: dict[str, TaskRecord]
) -> bool:
    stack = [dependency_id]
    seen: set[str] = set()
    while stack:
        current_id = stack.pop()
        if current_id == task_id:
            return True
        if current_id in seen:
            continue
        seen.add(current_id)
        current = tasks_by_id.get(current_id)
        if current is None:
            continue
        stack.extend(current.depends_on)
    return False


def _dependent_task_count(
    task_id: str, queue: list[str], tasks_by_id: dict[str, TaskRecord]
) -> int:
    eligible_task_ids = {
        queued_id
        for queued_id in queue
        if (
            (queued_task := tasks_by_id.get(queued_id)) is not None
            and _is_task_eligible_for_execution(queued_task)
        )
    }
    reverse_dependencies: dict[str, set[str]] = {candidate_id: set() for candidate_id in eligible_task_ids}
    for queued_id in eligible_task_ids:
        queued_task = tasks_by_id[queued_id]
        for dependency_id in queued_task.depends_on:
            if dependency_id in reverse_dependencies:
                reverse_dependencies[dependency_id].add(queued_id)

    count = 0
    seen: set[str] = set()
    stack = list(reverse_dependencies.get(task_id, ()))
    while stack:
        dependent_id = stack.pop()
        if dependent_id in seen:
            continue
        seen.add(dependent_id)
        count += 1
        stack.extend(reverse_dependencies.get(dependent_id, ()))
    return count


def _is_interrupted_task(task: TaskRecord) -> bool:
    return _is_task_eligible_for_execution(task) and (
        task.status == "in_progress" or task.pipeline_status != "backlog"
    )


def _task_selection_key(
    task: TaskRecord,
    *,
    queue_index: int,
    queue: list[str],
    tasks_by_id: dict[str, TaskRecord],
    policy: str,
) -> tuple[int | str, ...]:
    interrupted_rank = 0 if _is_interrupted_task(task) else 1
    if policy == "fifo":
        return (interrupted_rank, queue_index, task.id)
    if policy == "priority_first":
        return (interrupted_rank, TASK_PRIORITY_ORDER.get(task.priority, 1), queue_index, task.id)
    if policy == "dependency_aware":
        return (
            interrupted_rank,
            -_dependent_task_count(task.id, queue, tasks_by_id),
            queue_index,
            task.id,
        )
    raise ValueError(f"Unsupported pool selection policy '{policy}'")


def _resolve_next_task_from_state(
    root: Path, state: WorkspaceState
) -> tuple[TaskRecord | None, list[BlockedTask], bool]:
    mutated = False
    blocked: list[BlockedTask] = []
    blocked_task_ids: set[str] = set()
    tasks_by_id = {task.id: task for task in list_tasks(root)}
    policy = load_config(root).pool_selection_policy
    if policy not in VALID_POOL_SELECTION_POLICIES:
        policy = "dependency_aware"

    if state.active_task_id is not None:
        active_task = tasks_by_id.get(state.active_task_id)
        if active_task is not None and _is_task_eligible_for_execution(active_task):
            blockers = _task_blockers(active_task, tasks_by_id)
            if not blockers:
                return active_task, blocked, mutated
            if active_task.id not in state.queue:
                state.queue.insert(0, active_task.id)
            blocked.append(
                BlockedTask(
                    task_id=active_task.id,
                    title=active_task.title,
                    queue_position=1,
                    blocked_by=blockers,
                )
            )
            blocked_task_ids.add(active_task.id)
        state.active_task_id = None
        mutated = True

    ready_candidates: list[tuple[tuple[int, int, str], TaskRecord]] = []
    for index, next_id in enumerate(list(state.queue), start=1):
        next_task = tasks_by_id.get(next_id)
        if next_task is None or not _is_task_eligible_for_execution(next_task):
            state.queue.remove(next_id)
            mutated = True
            continue
        blockers = _task_blockers(next_task, tasks_by_id)
        if blockers:
            if next_task.id not in blocked_task_ids:
                blocked.append(
                    BlockedTask(
                        task_id=next_task.id,
                        title=next_task.title,
                        queue_position=index,
                        blocked_by=blockers,
                    )
                )
                blocked_task_ids.add(next_task.id)
            continue
        ready_candidates.append(
            (
                _task_selection_key(
                    next_task,
                    queue_index=index,
                    queue=list(state.queue),
                    tasks_by_id=tasks_by_id,
                    policy=policy,
                ),
                next_task,
            )
        )

    if ready_candidates:
        ready_candidates.sort(key=lambda item: item[0])
        return ready_candidates[0][1], blocked, mutated

    return None, blocked, mutated


def clear_active_task(root: Path) -> WorkspaceState:
    return set_active_task(root, None)


def restore_untouched_active_task(root: Path) -> WorkspaceState:
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        _validate_single_active_task(root, state)
        if state.active_task_id is None:
            return state

        task = get_task(root, state.active_task_id)
        if task is not None and task.runtime.execution_status == "running":
            return state
        if task is not None and _is_task_eligible_for_execution(task):
            task.status = "queued"
            save_task(root, task)
            state.queue = [item for item in state.queue if item != task.id]
            state.queue.insert(0, task.id)

        state.active_task_id = None
        save_state(root, state)
        return state


def enqueue_task(root: Path, task_id: str) -> WorkspaceState:
    return _enqueue_task(root, task_id, front=False)


def enqueue_task_front(root: Path, task_id: str) -> WorkspaceState:
    return _enqueue_task(root, task_id, front=True)


def _enqueue_task(root: Path, task_id: str, *, front: bool) -> WorkspaceState:
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        state.active_task_id = None
        state.queue = [item for item in state.queue if item != task_id]
        if front:
            state.queue.insert(0, task_id)
        else:
            state.queue.append(task_id)
        save_state(root, state)
        return state


def move_queued_task(root: Path, task_id: str, position: int) -> WorkspaceState:
    if position < 1:
        raise ValueError("Queue position must be 1 or greater")
    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        if task_id not in state.queue:
            raise ValueError(f"Task {task_id} is not queued")
        queue = [item for item in state.queue if item != task_id]
        target_index = min(position - 1, len(queue))
        queue.insert(target_index, task_id)
        state.queue = queue
        save_state(root, state)
        return state


def requeue_task(root: Path, task_id: str, *, front: bool = False) -> TaskRecord:
    with workspace_mutation_guard(root), _workspace_lock(root):
        task = require_task(root, task_id)
        if task.status not in {"flagged", "cancelled", "failed"}:
            raise ValueError(f"Task {task.id} is not flagged, failed, or cancelled")
        task.status = "queued"
        task.pipeline_status = "implementing"
        task.runtime.last_outcome = TaskOutcomeState()
        task.runtime.retry_count = 0
        task.runtime.retry_limit = 0
        task.runtime.retry_source = "global"
        append_journal(root, task, "Task requeued for another implementation pass.")
        save_task(root, task)

        state = load_state(root)
        state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        if front:
            state.queue.insert(0, task.id)
        else:
            state.queue.append(task.id)
        save_state(root, state)
        return task


def update_task_metadata(
    root: Path,
    task_id: str,
    *,
    depends_on: list[str] | object = ...,
    engine: str | None | object = ...,
    retry_limit: int | None | object = ...,
    priority: str | object = ...,
    goal: str | object = ...,
    mode: str | object = ...,
    auto_commit: bool | object = ...,
) -> TaskRecord:
    with workspace_mutation_guard(root), _workspace_lock(root):
        task = require_task(root, task_id)

        if depends_on is not ...:
            _validate_task_dependencies(root, task_id=task.id, depends_on=list(depends_on))
            task.depends_on = list(depends_on)

        if engine is not ...:
            if engine is not None and engine not in VALID_TASK_ENGINES:
                raise ValueError(f"Unsupported engine '{engine}'")
            task.engine = engine

        if retry_limit is not ...:
            if retry_limit is not None and retry_limit < 0:
                raise ValueError("Retry limit must be 0 or greater")
            task.retry_policy.max_retries = retry_limit

        if priority is not ...:
            if priority not in VALID_TASK_PRIORITIES:
                raise ValueError(f"Unsupported priority '{priority}'")
            task.priority = priority

        if goal is not ...:
            task.goal = goal

        if mode is not ...:
            if mode not in {"tasks", "implementation"}:
                raise ValueError(f"Unsupported mode '{mode}'")
            task.mode = mode  # type: ignore[assignment]

        if auto_commit is not ...:
            task.git.auto_commit = auto_commit

        append_journal(root, task, "Task metadata updated via CLI.")
        save_task(root, task)
        return task


def active_task_markers(
    root: Path, state: WorkspaceState | None = None
) -> dict[str, list[str]]:
    markers: dict[str, list[str]] = {}
    current_state = state or load_state(root)
    if current_state.active_task_id is not None:
        markers.setdefault(current_state.active_task_id, []).append("workspace.active_task_id")
    for task in list_tasks(root):
        if task.runtime.execution_status == "running":
            markers.setdefault(task.id, []).append("runtime.execution_status=running")
    return markers


def _validate_single_active_task(root: Path, state: WorkspaceState | None = None) -> None:
    markers = active_task_markers(root, state)
    if len(markers) <= 1:
        return
    details = "; ".join(
        f"{task_id} ({', '.join(task_markers)})" for task_id, task_markers in sorted(markers.items())
    )
    raise WorkspaceConflictError(
        f"workspace has multiple active tasks: {details}. Clear the stale active task state before running again."
    )
