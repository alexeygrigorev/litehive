"""Path helpers for task directories and artifacts."""

import gzip
import re
from pathlib import Path

from litehive.config.paths import workspace_dir, workspace_logs_dir
from litehive.config.workspace import ensure_workspace, resolve_workspace
from litehive.config.registry import list_registered_workspace_paths
from litehive.config.paths import worktree_root
from litehive.domain.task import TaskRecord


def _worktree_workspace_dir(root: Path) -> Path | None:
    resolved = root.resolve()
    parts = resolved.parts
    for i, part in enumerate(parts):
        if part == ".litehive" and i + 2 < len(parts) and parts[i + 1] == "worktrees":
            return Path(*parts[: i + 3]) / ".litehive"
    for registered_root in list_registered_workspace_paths():
        try:
            if resolved.is_relative_to(worktree_root(registered_root).resolve()):
                return workspace_dir(registered_root.resolve())
        except OSError:
            continue
    return None


def tasks_root(root: Path) -> Path:
    worktree_workspace = _worktree_workspace_dir(root)
    if worktree_workspace is not None:
        return worktree_workspace / "tasks"
    ensure_workspace(root)
    return workspace_dir(root) / "tasks"


def runner_lock_path(root: Path) -> Path:
    root = resolve_workspace(None, workspace=root)
    ensure_workspace(root)
    return workspace_dir(root) / ".runner.lock"


def slugify(value: str, max_length: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        return "task"
    if len(slug) <= max_length:
        return slug
    truncated = slug[:max_length].rsplit("-", 1)[0]
    return truncated.strip("-") or slug[:max_length].strip("-")


def task_dir(root: Path, task: TaskRecord) -> Path:
    return tasks_root(root) / f"{task.id}-{task.slug}"


def task_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "task.yaml"
def task_comments_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "comments.yaml"


def task_thread_file(root: Path, task: TaskRecord) -> Path:
    """Backward-compatible alias for the canonical task discussion file."""
    return task_comments_file(root, task)


def task_recovery_dir(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "recovery"


def latest_path(paths: list[Path]) -> Path | None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    return sorted(existing)[-1]


def _artifact_candidates(base: Path, *names: str) -> list[Path]:
    candidates: list[Path] = []
    for name in names:
        path = base / name
        candidates.append(path)
        if path.suffix != ".gz":
            candidates.append(base / f"{name}.gz")
    return candidates


def resolve_artifact_path(base: Path, *names: str) -> Path | None:
    for candidate in _artifact_candidates(base, *names):
        if candidate.exists():
            return candidate
    return None


def read_text_artifact(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8")


def latest_run_all_log_path(root: Path) -> Path | None:
    logs_root = workspace_logs_dir(root.resolve()) / "run-all"
    if not logs_root.exists():
        return None
    candidates = [
        path
        for path in logs_root.rglob("*")
        if path.is_file() and (path.suffix == ".log" or path.name.endswith(".log.gz"))
    ]
    if not candidates:
        return None
    return sorted(candidates)[-1]


def latest_subagent_base(root: Path, task: TaskRecord) -> Path | None:
    refs = list(task.subagents)
    preferred = []
    if task.runtime.active_subagent is not None:
        preferred.append(task.runtime.active_subagent.path)
    if task.runtime.last_subagent is not None:
        preferred.append(task.runtime.last_subagent.path)
    preferred.extend(ref.path for ref in refs)
    for rel_path in reversed(preferred):
        base = task_dir(root, task) / rel_path
        if base.exists():
            return base
    subagents_root = task_dir(root, task) / "subagents"
    if not subagents_root.exists():
        return None
    candidates = [path for path in subagents_root.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return sorted(candidates)[-1]


def status_entry_paths(entries: list[str]) -> list[str]:
    paths: list[str] = []
    for entry in entries:
        stripped = entry.strip()
        if not stripped:
            continue
        if " -> " in stripped:
            stripped = stripped.split(" -> ", 1)[1]
        if len(stripped) > 3:
            stripped = stripped[3:]
        paths.append(stripped)
    return paths
