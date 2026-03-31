"""Task storage helpers for the local YAML workspace."""

from __future__ import annotations

import fcntl
import re
from contextlib import contextmanager
from pathlib import Path

import yaml

from litehive.config import ensure_workspace, state_path, workspace_dir
from litehive.models import TaskRecord, WorkspaceState, utcnow


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
        yaml.safe_dump({"git": {"commit_sha": task.git.commit_sha}}, sort_keys=False),
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
    git = data.get("git") or {}
    task.git.commit_sha = git.get("commit_sha")
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


def save_task(root: Path, task: TaskRecord) -> None:
    task.updated_at = utcnow()
    task_payload = task.model_dump(mode="python")
    task_payload["git"]["commit_sha"] = None
    task_file(root, task).write_text(
        yaml.safe_dump(task_payload, sort_keys=False),
        encoding="utf-8",
    )
    _write_task_runtime(root, task)


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


def dequeue_next_task(root: Path) -> TaskRecord | None:
    state = load_state(root)
    if state.active_task_id:
        return get_task(root, state.active_task_id)
    if not state.queue:
        return None
    next_id = state.queue[0]
    set_active_task(root, next_id)
    return get_task(root, next_id)


def clear_active_task(root: Path) -> WorkspaceState:
    return set_active_task(root, None)


def enqueue_task(root: Path, task_id: str) -> WorkspaceState:
    with _workspace_lock(root):
        state = load_state(root)
        state.active_task_id = None
        if task_id not in state.queue:
            state.queue.append(task_id)
        save_state(root, state)
        return state
