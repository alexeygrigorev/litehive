"""Path helpers for task directories and artifacts."""

import gzip
import re
from pathlib import Path

from litehive.config.paths import workspace_path
from litehive.config.workspace_files import workspace_dir
from litehive.config.workspace import ensure_workspace, normalize_workspace_root, registered_workspace_root
from litehive.domain.task import TaskRecord


def _worktree_workspace_dir(root: Path) -> Path | None:
    """Resolve the canonical ``.litehive`` directory when ``root`` actually points inside a managed worktree; lets task-artifact helpers redirect writes back to the per-worktree workspace instead of polluting the main checkout."""
    resolved = root.resolve()
    parts = resolved.parts
    for i, part in enumerate(parts):
        if part == ".litehive" and i + 2 < len(parts) and parts[i + 1] == "worktrees":
            return Path(*parts[: i + 3]) / ".litehive"
    registered_root = registered_workspace_root(resolved)
    if registered_root is None:
        return None
    return workspace_dir(registered_root)


def tasks_root(root: Path, bootstrap: bool = True) -> Path:
    """Resolve the on-disk tasks/ directory for a workspace, transparently redirecting through the per-worktree .litehive/ when called from inside a worktree so artifacts land next to the right checkout."""
    worktree_workspace = _worktree_workspace_dir(root)
    if worktree_workspace is not None:
        return worktree_workspace / "tasks"
    if bootstrap:
        ensure_workspace(root)
    tasks = workspace_dir(root) / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    return tasks


def runner_lock_path(root: Path) -> Path:
    """Return the canonical runner lock file path; callers use this to claim or inspect the single-runner-per-workspace invariant."""
    root = normalize_workspace_root(root, source="runner_lock_path")
    ensure_workspace(root)
    return workspace_path(root, "runtime", ".runner.lock")


def slugify(value: str, max_length: int = 50) -> str:
    """Build a stable filesystem-safe slug for a task title; the slug is part of the on-disk task directory name, so the result must be deterministic and never empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        return "task"
    if len(slug) <= max_length:
        return slug
    truncated = slug[:max_length].rsplit("-", 1)[0]
    return truncated.strip("-") or slug[:max_length].strip("-")


def task_dir(root: Path, task: TaskRecord, bootstrap: bool = True) -> Path:
    """Return the per-task working directory (subagent transcripts, activity log, recovery evidence); the id-slug naming is the contract every other artifact helper assumes."""
    return tasks_root(root, bootstrap=bootstrap) / f"{task.id}-{task.slug}"


def task_recovery_dir(root: Path, task: TaskRecord, bootstrap: bool = True) -> Path:
    """Return the directory where recovery artifacts (probe outputs, repair plans) are written so recovery and downstream stages share one location."""
    return task_dir(root, task, bootstrap=bootstrap) / "recovery"


def latest_path(paths: list[Path]) -> Path | None:
    """Pick the lexicographically newest existing path from a candidate list; used by artifact lookups where filenames are timestamp-prefixed so sort order matches recency."""
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    return sorted(existing)[-1]


def _artifact_candidates(base: Path, *names: str) -> list[Path]:
    """Expand each candidate name into both its plain and ``.gz`` form so artifact lookups don't have to know whether the producer compressed the file; the order matters because ``resolve_artifact_path`` returns the first hit."""
    candidates: list[Path] = []
    for name in names:
        path = base / name
        candidates.append(path)
        if path.suffix != ".gz":
            candidates.append(base / f"{name}.gz")
    return candidates


def resolve_artifact_path(base: Path, *names: str) -> Path | None:
    """Find the first existing artifact under base, accepting both plain and .gz variants so callers don't need to know whether the producer compressed the output."""
    for candidate in _artifact_candidates(base, *names):
        if candidate.exists():
            return candidate
    return None


def read_text_artifact(path: Path) -> str:
    """Read a text artifact transparently whether or not it was gzipped on disk; pairs with resolve_artifact_path so the .gz format flip stays invisible to callers."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8")


def latest_run_all_log_path(root: Path) -> Path | None:
    """Find the most recent run-all log file for the workspace; called by the operator-facing logs CLI to surface the last batch run without making the operator know the timestamped layout."""
    logs_root = workspace_path(root.resolve(), "logs", "run-all")
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
    """Find the most relevant subagent artifact directory for a task, preferring the active subagent and falling back to the newest historical one; used by recovery and engine-switch flows that need the last attempt's transcript."""
    refs = list(task.subagents)
    preferred = []
    if task.runtime.execution.active_subagent is not None:
        preferred.append(task.runtime.execution.active_subagent.path)
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
    """Extract the path component from `git status --porcelain` lines, handling rename arrows and the leading status code so callers can compare paths to other lists."""
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
