"""Persistent operator-attention queue backed by the runtime database."""

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from litehive.config.workspace import ensure_workspace
from litehive.db.schema import connect_workspace_db
from litehive.models.common import utcnow
from litehive.models.task_models import TaskRecord
from litehive.state.records import list_tasks
from litehive.tasks.paths import tasks_root
from litehive.tasks.persistence import load_state, set_pool_stop_reason


DETECTABLE_ATTENTION_KINDS = {
    "duplicate_task_id",
    "flagged_task",
    "human_checkpoint_before_commit",
    "merge_failed_task",
    "origin_divergence",
    "stale_worktree",
}

ATTENTION_PRIORITIES = {
    "origin_divergence": 0,
    "destructive_git_denied": 1,
    "human_checkpoint_before_commit": 2,
    "merge_failed_task": 3,
    "duplicate_task_id": 4,
    "stale_worktree": 5,
    "flagged_task": 6,
}


class AttentionItem(BaseModel):
    id: int | None = None
    task_id: str | None = None
    created_at: str = Field(default_factory=utcnow)
    kind: str
    title: str
    reason: str
    suggested_action: str
    dedupe_key: str
    status: Literal["pending", "resolved"] = "pending"
    resolved_at: str | None = None
    resolution: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def append_attention_log(workspace: Path, message: str) -> None:
    path = workspace / ".litehive" / "runtime" / "attention.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utcnow()}\t{message}\n")


def attention_priority(item: AttentionItem) -> tuple[int, str, int]:
    return (ATTENTION_PRIORITIES.get(item.kind, 100), item.created_at, item.id or 0)


class AttentionStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def list_items(self, *, include_resolved: bool = False) -> list[AttentionItem]:
        with connect_workspace_db(self.root) as connection:
            rows = connection.execute(
                "SELECT id, task_id, created_at, kind, payload FROM attention ORDER BY id ASC"
            ).fetchall()
        items = [self._row_to_item(row) for row in rows]
        if include_resolved:
            return items
        return [item for item in items if item.status == "pending"]

    def create_or_keep(self, item: AttentionItem) -> AttentionItem:
        with connect_workspace_db(self.root) as connection:
            existing = self._find_pending_by_dedupe(connection, item.dedupe_key)
            if existing is not None:
                return self._row_to_item(existing)
            connection.execute(
                """
                INSERT INTO attention (task_id, created_at, kind, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    item.task_id,
                    item.created_at,
                    item.kind,
                    json.dumps(item.model_dump(mode="python", exclude={"id"}), sort_keys=True),
                ),
            )
            row_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.commit()
        item.id = row_id
        return item

    def resolve(self, item_id: int, *, resolution: str) -> AttentionItem | None:
        with connect_workspace_db(self.root) as connection:
            row = connection.execute(
                "SELECT id, task_id, created_at, kind, payload FROM attention WHERE id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            item = self._row_to_item(row)
            if item.status == "resolved":
                return item
            item.status = "resolved"
            item.resolved_at = utcnow()
            item.resolution = resolution
            if resolution == "resolved by operator" and item.kind in DETECTABLE_ATTENTION_KINDS:
                item.metadata["suppressed_until_cleared"] = True
                item.metadata["condition_cleared_after_resolution"] = False
            self._update_item(connection, item)
            connection.commit()
            return item

    def reconcile(self, detected: list[AttentionItem]) -> list[AttentionItem]:
        all_items = self.list_items(include_resolved=True)
        latest_by_key: dict[str, AttentionItem] = {}
        for item in all_items:
            latest_by_key[item.dedupe_key] = item
        pending = [item for item in all_items if item.status == "pending"]
        pending_by_key = {item.dedupe_key: item for item in pending}
        detected_keys = {item.dedupe_key for item in detected}

        for item in detected:
            if item.dedupe_key in pending_by_key:
                continue
            previous = latest_by_key.get(item.dedupe_key)
            if previous is not None and self._is_operator_suppressed(previous):
                continue
            self.create_or_keep(item)

        with connect_workspace_db(self.root) as connection:
            for item in all_items:
                if item.kind not in DETECTABLE_ATTENTION_KINDS:
                    continue
                if item.dedupe_key in detected_keys:
                    continue
                if item.status == "pending":
                    item.status = "resolved"
                    item.resolved_at = utcnow()
                    item.resolution = "auto-resolved by attention reconciliation"
                    self._update_item(connection, item)
                    continue
                if self._is_operator_suppressed(item):
                    item.metadata["condition_cleared_after_resolution"] = True
                    self._update_item(connection, item)
            connection.commit()

        return self.list_items(include_resolved=False)

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> AttentionItem:
        payload = json.loads(row["payload"])
        payload.setdefault("created_at", row["created_at"])
        payload.setdefault("kind", row["kind"])
        payload.setdefault("task_id", row["task_id"])
        payload["id"] = int(row["id"])
        return AttentionItem(**payload)

    @staticmethod
    def _find_pending_by_dedupe(connection: sqlite3.Connection, dedupe_key: str) -> sqlite3.Row | None:
        rows = connection.execute(
            "SELECT id, task_id, created_at, kind, payload FROM attention ORDER BY id ASC"
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("status", "pending") != "pending":
                continue
            if payload.get("dedupe_key") == dedupe_key:
                return row
        return None

    @staticmethod
    def _is_operator_suppressed(item: AttentionItem) -> bool:
        return (
            item.status == "resolved"
            and item.resolution == "resolved by operator"
            and item.metadata.get("suppressed_until_cleared") is True
            and item.metadata.get("condition_cleared_after_resolution") is not True
        )

    @staticmethod
    def _update_item(connection: sqlite3.Connection, item: AttentionItem) -> None:
        connection.execute(
            "UPDATE attention SET payload = ? WHERE id = ?",
            (
                json.dumps(item.model_dump(mode="python", exclude={"id"}), sort_keys=True),
                item.id,
            ),
        )


def attention_store(root: Path) -> AttentionStore:
    return AttentionStore(root)


def record_attention(
    root: Path,
    *,
    kind: str,
    title: str,
    reason: str,
    suggested_action: str,
    task_id: str | None = None,
    dedupe_key: str | None = None,
    metadata: dict[str, Any] | None = None,
    log_message: str | None = None,
) -> AttentionItem:
    ensure_workspace(root)
    item = AttentionItem(
        task_id=task_id,
        kind=kind,
        title=title,
        reason=reason,
        suggested_action=suggested_action,
        dedupe_key=dedupe_key or _default_dedupe_key(kind, task_id=task_id, title=title, reason=reason),
        metadata=metadata or {},
    )
    stored = attention_store(root).create_or_keep(item)
    append_attention_log(root, log_message or f"{title}: {reason}")
    return stored


def list_attention(root: Path, *, reconcile: bool = True) -> list[AttentionItem]:
    ensure_workspace(root)
    if reconcile:
        return reconcile_attention(root)
    return sorted(attention_store(root).list_items(), key=attention_priority)


def resolve_attention(root: Path, item_id: int) -> AttentionItem | None:
    ensure_workspace(root)
    return attention_store(root).resolve(item_id, resolution="resolved by operator")


def reconcile_attention(root: Path) -> list[AttentionItem]:
    ensure_workspace(root)
    state = load_state(root)
    _import_attention_log_events(root)
    detected = _detect_attention_items(root, state.pool_stop_reason)
    unresolved = attention_store(root).reconcile(detected)
    if state.pool_stop_reason == "attention_required" and not unresolved:
        set_pool_stop_reason(root, None)
    return sorted(unresolved, key=attention_priority)


def waiting_for_you_lines(root: Path, *, limit: int = 5) -> list[str]:
    try:
        items = list_attention(root)
    except Exception:
        return ["attention_items: unavailable"]
    lines = [f"attention_items: {len(items)}"]
    if not items:
        return lines
    lines.append("waiting for you:")
    for item in items[:limit]:
        task_label = f" [{item.task_id}]" if item.task_id else ""
        lines.append(f"- ({item.id}) {item.title}{task_label}: {item.suggested_action}")
    return lines


def _detect_attention_items(root: Path, pool_stop_reason: str | None) -> list[AttentionItem]:
    tasks = list_tasks(root)
    state = load_state(root)
    detected: list[AttentionItem] = []
    detected.extend(_duplicate_id_items(root))
    detected.extend(_flagged_and_merge_failed_items(tasks))
    detected.extend(_stale_worktree_items(root, tasks, state))
    divergence = _origin_divergence_item(root)
    if divergence is not None:
        detected.append(divergence)
    checkpoint = _human_checkpoint_item(tasks, state.active_task_id, pool_stop_reason)
    if checkpoint is not None:
        detected.append(checkpoint)
    return detected


def _import_attention_log_events(root: Path) -> None:
    log_path = root / ".litehive" / "runtime" / "attention.log"
    if not log_path.exists():
        return
    seen_keys = {
        item.dedupe_key
        for item in attention_store(root).list_items(include_resolved=True)
    }
    try:
        entries = [line.strip() for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return
    for entry in entries:
        _, _, message = entry.partition("\t")
        if "merge-resolver git wrapper rejected `" not in message:
            continue
        prefix = "merge-resolver git wrapper rejected `"
        command = message.split(prefix, 1)[1].split("`:", 1)[0]
        rejection_reason = message.rsplit(": ", 1)[-1]
        dedupe_key = f"destructive_git_denied:{command}:{rejection_reason}"
        if dedupe_key in seen_keys:
            continue
        attention_store(root).create_or_keep(
            AttentionItem(
                kind="destructive_git_denied",
                title="Destructive git command was blocked",
                reason=f"`{command}` was rejected: {rejection_reason}",
                suggested_action=(
                    "Use a non-destructive git recovery path instead. Once reviewed,"
                    " clear the queue item with `litehive attention resolve <id>`."
                ),
                dedupe_key=dedupe_key,
                metadata={"command": command, "rejection_reason": rejection_reason},
            )
        )
        seen_keys.add(dedupe_key)


def _duplicate_id_items(root: Path) -> list[AttentionItem]:
    counts: dict[str, int] = {}
    for child in sorted(tasks_root(root).iterdir()):
        if not child.is_dir():
            continue
        task_path = child / "task.yaml"
        if not task_path.exists():
            continue
        try:
            payload = yaml.safe_load(task_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        task_id = str(payload.get("id") or "").strip()
        if not task_id:
            continue
        counts[task_id] = counts.get(task_id, 0) + 1
    items: list[AttentionItem] = []
    for task_id, count in sorted(counts.items()):
        if count <= 1:
            continue
        items.append(
            AttentionItem(
                task_id=task_id,
                kind="duplicate_task_id",
                title=f"Duplicate task id detected for {task_id}",
                reason=f"{count} task directories claim the same id.",
                suggested_action="Resolve the duplicate task directories manually before running the daemon again.",
                dedupe_key=f"duplicate_task_id:{task_id}",
                metadata={"count": count},
            )
        )
    return items


def _flagged_and_merge_failed_items(tasks: list[TaskRecord]) -> list[AttentionItem]:
    items: list[AttentionItem] = []
    for task in tasks:
        if task.status == "flagged":
            items.append(
                AttentionItem(
                    task_id=task.id,
                    kind="flagged_task",
                    title=f"Task {task.id} is flagged",
                    reason=(
                        f"Task is paused in `{task.pipeline_status}` and requires operator review."
                        f" Flag reason: {task.flag_reason or 'unknown'}."
                    ),
                    suggested_action=f"Run `litehive debug {task.id}` and then `litehive queue promote {task.id}` when it is ready to continue.",
                    dedupe_key=f"flagged_task:{task.id}",
                    metadata={"pipeline_status": task.pipeline_status, "flag_reason": task.flag_reason},
                )
            )
        if task.status == "merge_failed":
            items.append(
                AttentionItem(
                    task_id=task.id,
                    kind="merge_failed_task",
                    title=f"Task {task.id} needs merge recovery",
                    reason="Checkpoint commit or merge resolution failed and the managed worktree needs operator recovery.",
                    suggested_action=f"Run `litehive debug {task.id} --worktree` and then `litehive recover {task.id}`.",
                    dedupe_key=f"merge_failed_task:{task.id}",
                    metadata={"pipeline_status": task.pipeline_status},
                )
            )
    return items


def _stale_worktree_items(root: Path, tasks: list[TaskRecord], state) -> list[AttentionItem]:
    from litehive.config.paths import worktree_root
    from litehive.tasks.queue import is_task_eligible_for_execution
    from litehive.tasks.worktrees import (
        is_managed_worktree_path,
        resolve_recorded_worktree_path,
    )

    items: list[AttentionItem] = []
    managed_paths: dict[str, str | None] = {}
    active_task_id = None if state is None else state.active_task_id
    for task in tasks:
        worktree_rel = task.runtime.git.worktree_path or task.git.worktree_path
        if not is_managed_worktree_path(root, worktree_rel):
            continue
        worktree_path = resolve_recorded_worktree_path(root, worktree_rel)
        if worktree_rel is None:
            continue
        managed_paths[worktree_rel] = task.id
        if worktree_path is None or not worktree_path.exists():
            continue
        if active_task_id == task.id or is_task_eligible_for_execution(task):
            continue
        items.append(
            AttentionItem(
                task_id=task.id,
                kind="stale_worktree",
                title="Managed worktree needs cleanup",
                reason=f"task_id={task.id} status={task.status} path={worktree_rel}",
                suggested_action="Run `litehive worktree clean --dry-run` and then `litehive worktree clean`.",
                dedupe_key=f"stale_worktree:{worktree_rel}",
                metadata={"path": worktree_rel},
            )
        )
    worktrees_root = worktree_root(root)
    if worktrees_root.exists():
        for child in sorted(worktrees_root.iterdir()):
            if not child.is_dir():
                continue
            rel = str(child)
            if rel in managed_paths:
                continue
            items.append(
                AttentionItem(
                    task_id=None,
                    kind="stale_worktree",
                    title="Managed worktree needs cleanup",
                    reason=f"task_id=missing path={rel}",
                    suggested_action="Run `litehive worktree clean --dry-run` and then `litehive worktree clean`.",
                    dedupe_key=f"stale_worktree:{rel}",
                    metadata={"path": rel},
                )
            )
    return items


def _origin_divergence_item(root: Path) -> AttentionItem | None:
    from litehive.daemon.execution import check_origin_divergence

    message = check_origin_divergence(root)
    if message is None:
        return None
    return AttentionItem(
        kind="origin_divergence",
        title="Local main has diverged from origin/main",
        reason=message,
        suggested_action=(
            "Run `git fetch origin main && git log --oneline --left-right main...origin/main`,"
            " then rebase, reset, or merge before restarting the pool."
        ),
        dedupe_key="origin_divergence:main",
    )


def _human_checkpoint_item(
    tasks: list[TaskRecord],
    active_task_id: str | None,
    pool_stop_reason: str | None,
) -> AttentionItem | None:
    if pool_stop_reason != "human_checkpoint_before_commit":
        return None
    task = next((candidate for candidate in tasks if candidate.id == active_task_id), None)
    title = "Human checkpoint before commit reached"
    reason = "Pool paused at the task's configured checkpoint before commit."
    task_id = active_task_id
    if task is not None:
        reason = f"Task {task.id} reached `human_checkpoint_before_commit` and needs operator review before commit."
        task_id = task.id
    action = (
        f"Run `litehive debug {task_id} --worktree` to inspect the task, then continue with `litehive run`"
        " or roll it back with `litehive rollback`."
        if task_id
        else "Inspect the active task and then continue with `litehive run` or `litehive rollback`."
    )
    return AttentionItem(
        task_id=task_id,
        kind="human_checkpoint_before_commit",
        title=title,
        reason=reason,
        suggested_action=action,
        dedupe_key=f"human_checkpoint_before_commit:{task_id or 'workspace'}",
    )


def _default_dedupe_key(kind: str, *, task_id: str | None, title: str, reason: str) -> str:
    if task_id:
        return f"{kind}:{task_id}"
    return f"{kind}:{title}:{reason}"
