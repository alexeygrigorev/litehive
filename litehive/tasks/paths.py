"""Path helpers for task directories and artifacts."""

import gzip
import re
from pathlib import Path

from litehive.config import ensure_workspace, workspace_dir
from litehive.models import TaskRecord


def tasks_root(root: Path) -> Path:
    ensure_workspace(root)
    return workspace_dir(root) / "tasks"


def runner_lock_path(root: Path) -> Path:
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


def task_runtime_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "runtime.yaml"


def task_thread_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "thread.yaml"


def task_recovery_dir(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "recovery"


def _latest_path(paths: list[Path]) -> Path | None:
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


def _resolve_artifact_path(base: Path, *names: str) -> Path | None:
    for candidate in _artifact_candidates(base, *names):
        if candidate.exists():
            return candidate
    return None


def _read_text_artifact(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8")


def _latest_run_all_log_path(root: Path) -> Path | None:
    logs_root = root / ".litehive" / "logs" / "run-all"
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


def _latest_subagent_base(root: Path, task: TaskRecord) -> Path | None:
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


def _status_entry_paths(entries: list[str]) -> list[str]:
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
