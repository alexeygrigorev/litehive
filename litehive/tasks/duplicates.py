"""Duplicate task detection and search backed by a workspace-local sqlitesearch index."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from math import ceil
from pathlib import Path
import re

from sqlitesearch import TextSearchIndex

from litehive.config.paths import workspace_path
from litehive.domain.task import TaskRecord

_INDEX_FILENAME = "task-duplicates.db"
_INDEX_DIRNAME = "search"
_MATCH_STATUSES = {"queued", "in_progress", "done", "archived"}
_TASK_ID_RE = re.compile(r"^T-\d+$", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class DuplicateTaskMatch:
    task_id: str
    title: str
    status: str


@dataclass(frozen=True)
class TaskSearchMatch:
    task_id: str
    title: str
    status: str
    snippet: str


def duplicate_index_path(root: Path) -> Path:
    return workspace_path(root, _INDEX_DIRNAME, _INDEX_FILENAME)


def duplicate_index_exists(root: Path) -> bool:
    return duplicate_index_path(root).exists()


def ensure_duplicate_task_index(root: Path) -> Path:
    if duplicate_index_exists(root):
        return duplicate_index_path(root)
    return rebuild_duplicate_task_index(root)


def rebuild_duplicate_task_index(root: Path) -> Path:
    documents = [_task_to_document(task) for task in iter_indexable_tasks(root)]
    index = _open_duplicate_index(root)
    try:
        index.clear()
        if documents:
            index.fit(documents)
    finally:
        index.close()
    return duplicate_index_path(root)


def add_tasks_to_duplicate_index(root: Path, tasks: Iterable[TaskRecord]) -> None:
    if not duplicate_index_exists(root):
        return
    if any(task.status in _MATCH_STATUSES for task in tasks):
        rebuild_duplicate_task_index(root)


def refresh_duplicate_task_index_if_initialized(root: Path) -> None:
    if not duplicate_index_exists(root):
        return
    rebuild_duplicate_task_index(root)


def find_potential_duplicate_tasks(
    root: Path,
    *,
    title: str,
    goal: str = "",
    limit: int = 5,
) -> list[DuplicateTaskMatch]:
    ensure_duplicate_task_index(root)
    query = _search_query(title, goal)
    if not query:
        return []

    title_norm = _normalize_text(title)
    goal_norm = _normalize_text(goal)
    title_tokens = _tokens(title)
    full_tokens = _tokens(f"{title} {goal}")

    index = _open_duplicate_index(root)
    try:
        raw_matches = index.search(
            query,
            boost_dict={"title": 3.0, "goal": 1.25},
            num_results=max(limit * 4, 10),
        )
    finally:
        index.close()

    ranked_matches: list[tuple[float, DuplicateTaskMatch]] = []
    seen: set[str] = set()
    for candidate in raw_matches:
        task_id = str(candidate.get("task_id", "")).strip()
        if not task_id or task_id in seen:
            continue
        status = str(candidate.get("status", "")).strip()
        if status not in _MATCH_STATUSES:
            continue
        candidate_title = str(candidate.get("title", ""))
        candidate_goal = str(candidate.get("goal", ""))
        duplicate_score = _duplicate_score(
            title_norm=title_norm,
            goal_norm=goal_norm,
            title_tokens=title_tokens,
            full_tokens=full_tokens,
            candidate_title=candidate_title,
            candidate_goal=candidate_goal,
        )
        if duplicate_score is None:
            continue
        seen.add(task_id)
        ranked_matches.append(
            (
                duplicate_score,
                DuplicateTaskMatch(
                    task_id=task_id,
                    title=candidate_title,
                    status=status,
                ),
            )
        )

    ranked_matches.sort(key=lambda item: (-item[0], item[1].task_id))
    return [match for _, match in ranked_matches[:limit]]


def search_tasks_by_text(root: Path, *, query: str, limit: int = 5) -> list[TaskSearchMatch]:
    if limit < 1:
        return []
    exact_match = _exact_task_id_match(root, query)
    if exact_match is not None:
        return [exact_match]

    normalized_query = _normalize_query(query)
    if not normalized_query:
        return []

    ensure_duplicate_task_index(root)
    index = _open_duplicate_index(root)
    try:
        raw_matches = index.search(
            normalized_query,
            boost_dict={"title": 3.0, "goal": 1.5},
            num_results=max(limit * 4, 10),
        )
    finally:
        index.close()

    results: list[TaskSearchMatch] = []
    seen: set[str] = set()
    for candidate in raw_matches:
        task_id = str(candidate.get("task_id", "")).strip()
        if not task_id or task_id in seen:
            continue
        status = str(candidate.get("status", "")).strip()
        if status not in _MATCH_STATUSES:
            continue
        title = str(candidate.get("title", "")).strip()
        goal = str(candidate.get("goal", "")).strip()
        seen.add(task_id)
        results.append(
            TaskSearchMatch(
                task_id=task_id,
                title=title,
                status=status,
                snippet=_search_snippet(title=title, goal=goal, query=normalized_query),
            )
        )
        if len(results) >= limit:
            break
    return results


def _exact_task_id_match(root: Path, query: str) -> TaskSearchMatch | None:
    task_id = query.strip().upper()
    if not _TASK_ID_RE.fullmatch(task_id):
        return None

    from litehive.state.records import get_task
    from litehive.tasks.archive import get_archived_task

    task = get_task(root, task_id)
    if task is None:
        task = get_archived_task(root, task_id)
    if task is None:
        return None
    snippet_query = task.goal or task.title or task.id
    return TaskSearchMatch(
        task_id=task.id,
        title=task.title,
        status=str(task.status),
        snippet=_search_snippet(title=task.title, goal=task.goal, query=snippet_query),
    )


def _open_duplicate_index(root: Path) -> TextSearchIndex:
    path = duplicate_index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    return TextSearchIndex(
        text_fields=["title", "goal"],
        keyword_fields=["task_id", "status"],
        id_field="task_id",
        db_path=str(path),
        stemming=True,
    )


def iter_indexable_tasks(root: Path) -> list[TaskRecord]:
    from litehive.state.records import list_tasks
    from litehive.tasks.archive import list_archived_tasks

    task_by_id: dict[str, TaskRecord] = {}
    for task in list_tasks(root, include_runtime=True, strict=False):
        if task.status in _MATCH_STATUSES:
            task_by_id[task.id] = task
    for task in list_archived_tasks(root):
        if task.status in _MATCH_STATUSES and task.id not in task_by_id:
            task_by_id[task.id] = task
    return list(task_by_id.values())


def _task_to_document(task: TaskRecord) -> dict[str, str]:
    return {
        "task_id": task.id,
        "title": task.title,
        "goal": task.goal,
        "status": task.status,
    }


def _search_query(title: str, goal: str) -> str:
    primary_tokens = _ordered_tokens(title)
    if not primary_tokens:
        primary_tokens = _ordered_tokens(goal)
    if not primary_tokens:
        return ""

    query_tokens = list(primary_tokens)
    for token in _ordered_tokens(goal):
        if token in primary_tokens or token in query_tokens:
            continue
        query_tokens.append(token)
        if len(query_tokens) >= 10:
            break
    return " ".join(query_tokens)


def _normalize_query(value: str) -> str:
    return " ".join(_ordered_tokens(value))


def _search_snippet(*, title: str, goal: str, query: str, max_chars: int = 100) -> str:
    source = " ".join(goal.split()) or " ".join(title.split())
    if len(source) <= max_chars:
        return source

    for token in _ordered_tokens(query):
        match_index = source.lower().find(token)
        if match_index == -1:
            continue
        start = max(0, match_index - 20)
        end = min(len(source), start + max_chars)
        snippet = source[start:end].strip()
        if start > 0:
            snippet = f"...{snippet}"
        if end < len(source):
            snippet = f"{snippet}..."
        return snippet
    return f"{source[: max_chars - 3].rstrip()}..."


def _duplicate_score(
    *,
    title_norm: str,
    goal_norm: str,
    title_tokens: set[str],
    full_tokens: set[str],
    candidate_title: str,
    candidate_goal: str,
) -> float | None:
    candidate_title_norm = _normalize_text(candidate_title)
    candidate_full_norm = _normalize_text(f"{candidate_title} {candidate_goal}")
    candidate_title_tokens = _tokens(candidate_title)
    candidate_full_tokens = _tokens(f"{candidate_title} {candidate_goal}")

    if not candidate_title_norm and not candidate_full_norm:
        return None

    title_ratio = SequenceMatcher(None, title_norm, candidate_title_norm).ratio() if title_norm else 0.0
    full_ratio = SequenceMatcher(
        None,
        _normalize_text(f"{title_norm} {goal_norm}".strip()),
        candidate_full_norm,
    ).ratio()
    title_overlap = len(title_tokens & candidate_title_tokens)
    full_overlap = len(full_tokens & candidate_full_tokens)
    title_coverage = title_overlap / len(title_tokens) if title_tokens else 0.0
    full_coverage = full_overlap / len(full_tokens) if full_tokens else 0.0

    if title_norm and title_norm == candidate_title_norm:
        return 1.0
    if title_ratio >= 0.76:
        return max(title_ratio, full_ratio)
    if title_tokens and title_overlap >= max(2, ceil(len(title_tokens) * 0.6)):
        return max(title_ratio, title_coverage, full_ratio)
    if full_tokens and full_overlap >= max(3, ceil(len(full_tokens) * 0.5)) and full_ratio >= 0.55:
        return max(full_ratio, full_coverage)
    if full_ratio >= 0.82:
        return full_ratio
    return None


def _normalize_text(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(value.lower()))


def _ordered_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall(value.lower()):
        if len(token) <= 2 or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _tokens(value: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall(value.lower()) if len(token) > 2}
