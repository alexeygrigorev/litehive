"""Path helpers for task directories and artifacts."""

import gzip
import re
from pathlib import Path

from litehive.config.paths import workspace_path
from litehive.config.workspace_files import workspace_dir
from litehive.config.workspace import create_workspace, normalize_workspace_root
from litehive.domain.task import TaskRecord
from litehive.workspace import Workspace


def _worktree_workspace_dir(root: Path) -> Path | None:
    """
    Resolve the canonical ``.litehive`` directory for a managed worktree.

    When ``root`` points inside a worktree this returns the per-worktree
    ``.litehive`` so task-artifact helpers redirect writes back to the
    matching workspace instead of polluting the main checkout's directory.
    """
    resolved = root.resolve()
    parts = resolved.parts
    for i, part in enumerate(parts):
        if part == ".litehive" and i + 2 < len(parts) and parts[i + 1] == "worktrees":
            return Path(*parts[: i + 3]) / ".litehive"
    return None


def tasks_root(root: Path, bootstrap: bool = True) -> Path:
    """
    Resolve the on-disk ``tasks/`` directory for a workspace.

    Transparently redirects through the per-worktree ``.litehive/`` when
    called from inside a worktree so artifacts land next to the right
    checkout; without the redirect, every worktree would write into the
    main repo and stomp each other's transcripts.
    """
    worktree_workspace = _worktree_workspace_dir(root)
    if worktree_workspace is not None:
        return worktree_workspace / "tasks"
    if bootstrap:
        create_workspace(root)
    tasks = workspace_dir(root) / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    return tasks


def runner_lock_path(root: Path) -> Path:
    """
    Return the canonical runner lock file path.

    Callers use this to claim or inspect the single-runner-per-workspace
    invariant; a stable, predictable path is required so external tools
    (deploy scripts, status panels) can read the lock without going through
    Python.
    """
    root = normalize_workspace_root(root, source="runner_lock_path")
    create_workspace(root)
    return workspace_path(root, "runtime", ".runner.lock")


def slugify(value: str, max_length: int = 50) -> str:
    """
    Build a stable filesystem-safe slug for a task title.

    The slug is part of the on-disk task directory name, so the result
    must be deterministic and never empty — falling back to the literal
    ``"task"`` when the title contains nothing slug-friendly.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        return "task"
    if len(slug) <= max_length:
        return slug
    truncated = slug[:max_length].rsplit("-", 1)[0]
    return truncated.strip("-") or slug[:max_length].strip("-")


def task_dir(root: Path, task: TaskRecord, bootstrap: bool = True) -> Path:
    """
    Return the per-task working directory.

    Holds subagent transcripts, the activity log, and recovery evidence; the
    ``<id>-<slug>`` naming is the contract every other artifact helper in
    this module assumes when constructing paths.
    """
    return tasks_root(root, bootstrap=bootstrap) / f"{task.id}-{task.slug}"


def task_recovery_dir(root: Path, task: TaskRecord, bootstrap: bool = True) -> Path:
    """
    Return the directory where recovery artifacts are written.

    Probe outputs and repair plans land here so recovery and the downstream
    stages reading those plans share one location instead of each subsystem
    inventing its own path scheme.
    """
    return task_dir(root, task, bootstrap=bootstrap) / "recovery"


def latest_path(paths: list[Path]) -> Path | None:
    """
    Pick the lexicographically newest existing path from a candidate list.

    Used by artifact lookups where filenames are timestamp-prefixed so sort
    order matches recency; non-existent candidates are filtered out so a
    deleted artifact does not bubble up as the "latest".
    """
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    return sorted(existing)[-1]


def _artifact_candidates(base: Path, *names: str) -> list[Path]:
    """
    Expand each candidate name into both its plain and ``.gz`` form.

    Artifact lookups don't have to know whether the producer compressed the
    file; the plain form is checked first so a freshly written uncompressed
    artifact wins over a stale gzipped one with the same root name.
    """
    candidates: list[Path] = []
    for name in names:
        path = base / name
        candidates.append(path)
        if path.suffix != ".gz":
            candidates.append(base / f"{name}.gz")
    return candidates


def resolve_artifact_path(base: Path, *names: str) -> Path | None:
    """
    Find the first existing artifact under ``base``.

    Accepts both plain and ``.gz`` variants so callers do not need to know
    whether the producer compressed the output; the order encoded in
    ``_artifact_candidates`` decides the precedence.
    """
    for candidate in _artifact_candidates(base, *names):
        if candidate.exists():
            return candidate
    return None


def read_text_artifact(path: Path) -> str:
    """
    Read a text artifact whether or not it was gzipped on disk.

    Pairs with ``resolve_artifact_path`` so the ``.gz`` format flip stays
    invisible to callers; subagent transcript readers, recovery evidence
    builders, and the operator log viewer all funnel through this helper.
    """
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8")


def latest_run_all_log_path(root: Path) -> Path | None:
    """
    Find the most recent ``run-all`` log file for the workspace.

    Called by the operator-facing logs CLI to surface the last batch run
    without forcing the operator to know the timestamped on-disk layout;
    walks the whole tree because the ``run-all`` log path is partitioned by
    date.
    """
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
    """
    Path-based compatibility wrapper for latest subagent lookup.
    """
    return latest_subagent_base_for_workspace(Workspace.from_path(root), task)


def latest_subagent_base_for_workspace(workspace: Workspace, task: TaskRecord) -> Path | None:
    """
    Find the most relevant subagent artifact directory for a task.

    Prefers the currently active subagent, then falls back to the newest
    historical one; used by recovery and the engine-switch flow that need
    the last attempt's transcript to seed the next agent's context.
    """
    task_base = workspace.task_dir(task)
    refs = list(task.subagents)
    preferred = []
    if task.runtime.execution.active_subagent is not None:
        preferred.append(task.runtime.execution.active_subagent.path)
    preferred.extend(ref.path for ref in refs)
    for rel_path in reversed(preferred):
        base = task_base / rel_path
        if base.exists():
            return base
    subagents_root = task_base / "subagents"
    if not subagents_root.exists():
        return None
    candidates = [path for path in subagents_root.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return sorted(candidates)[-1]


def status_entry_paths(entries: list[str]) -> list[str]:
    """
    Extract the path components from ``git status --porcelain`` lines.

    Strips the leading status code and rewrites rename arrows so callers
    can compare the result to plain path lists; recovery evidence and
    requeue-time validation both use the cleaned form.
    """
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
