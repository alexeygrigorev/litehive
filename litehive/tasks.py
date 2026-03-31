"""Task storage helpers for the local YAML workspace."""

from __future__ import annotations

import fcntl
import re
from contextlib import contextmanager
from pathlib import Path

import yaml

from litehive.config import ensure_workspace, state_path, workspace_dir
from litehive.models import (
    RuntimeEngineSwitch,
    RuntimeSubagentState,
    StageReport,
    SubagentRef,
    TaskRecord,
    TaskRuntime,
    WorkspaceState,
    utcnow,
)

VALID_TASK_PRIORITIES = {"low", "medium", "high"}
VALID_TASK_ENGINES = {"codex", "opencode", "gemini"}


def load_state(root: Path) -> WorkspaceState:
    ensure_workspace(root)
    data = yaml.safe_load(state_path(root).read_text(encoding="utf-8")) or {}
    return WorkspaceState(**data)


def save_state(root: Path, state: WorkspaceState) -> None:
    state_path(root).write_text(
        yaml.safe_dump(state.model_dump(mode="python"), sort_keys=False),
        encoding="utf-8",
    )


def tasks_root(root: Path) -> Path:
    ensure_workspace(root)
    return workspace_dir(root) / "tasks"


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
    mode: str = "implementation",
    engine: str | None = None,
    goal: str = "",
    acceptance_criteria: list[str] | None = None,
    auto_commit: bool = True,
) -> TaskRecord:
    ensure_workspace(root)
    with _workspace_lock(root):
        task_id = _next_task_id(root)
        slug = slugify(title)
        task = TaskRecord(
            id=task_id,
            slug=slug,
            title=title,
            engine=engine,
            mode=mode,  # type: ignore[arg-type]
            goal=goal,
            acceptance_criteria=acceptance_criteria or [],
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
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {utcnow()}\n{message}\n")


def set_active_task(root: Path, task_id: str | None) -> WorkspaceState:
    with _workspace_lock(root):
        state = load_state(root)
        state.active_task_id = task_id
        if task_id is not None and task_id in state.queue:
            state.queue = [item for item in state.queue if item != task_id]
        save_state(root, state)
        return state


def peek_next_task(root: Path) -> TaskRecord | None:
    with _workspace_lock(root):
        state = load_state(root)
        next_task, mutated = _resolve_next_task_from_state(root, state)
        if mutated:
            save_state(root, state)
        return next_task


def dequeue_next_task(root: Path) -> TaskRecord | None:
    with _workspace_lock(root):
        state = load_state(root)
        next_task, mutated = _resolve_next_task_from_state(root, state)
        if next_task is None:
            if mutated:
                save_state(root, state)
            return None
        if state.active_task_id != next_task.id:
            state.active_task_id = next_task.id
            state.queue = [item for item in state.queue if item != next_task.id]
            mutated = True
        if mutated:
            save_state(root, state)
        return next_task


def _is_task_eligible_for_execution(task: TaskRecord) -> bool:
    return task.status in {"queued", "in_progress"} and task.pipeline_status != "done"


def _resolve_next_task_from_state(root: Path, state: WorkspaceState) -> tuple[TaskRecord | None, bool]:
    mutated = False

    if state.active_task_id is not None:
        active_task = get_task(root, state.active_task_id)
        if active_task is not None and _is_task_eligible_for_execution(active_task):
            return active_task, mutated
        state.active_task_id = None
        mutated = True

    while state.queue:
        next_id = state.queue[0]
        next_task = get_task(root, next_id)
        if next_task is not None and _is_task_eligible_for_execution(next_task):
            return next_task, mutated
        state.queue.pop(0)
        mutated = True

    return None, mutated


def clear_active_task(root: Path) -> WorkspaceState:
    return set_active_task(root, None)


def enqueue_task(root: Path, task_id: str) -> WorkspaceState:
    return _enqueue_task(root, task_id, front=False)


def enqueue_task_front(root: Path, task_id: str) -> WorkspaceState:
    return _enqueue_task(root, task_id, front=True)


def _enqueue_task(root: Path, task_id: str, *, front: bool) -> WorkspaceState:
    with _workspace_lock(root):
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
    with _workspace_lock(root):
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
    with _workspace_lock(root):
        task = require_task(root, task_id)
        if task.status not in {"flagged", "cancelled"}:
            raise ValueError(f"Task {task.id} is not flagged or cancelled")
        task.status = "queued"
        task.pipeline_status = "implementing"
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
    engine: str | None | object = ...,
    priority: str | object = ...,
    goal: str | object = ...,
    mode: str | object = ...,
    auto_commit: bool | object = ...,
) -> TaskRecord:
    with _workspace_lock(root):
        task = require_task(root, task_id)

        if engine is not ...:
            if engine is not None and engine not in VALID_TASK_ENGINES:
                raise ValueError(f"Unsupported engine '{engine}'")
            task.engine = engine

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
