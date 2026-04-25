"""Persistent operator-attention queue backed by the workspace SQLite DB."""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from litehive.config.workspace import ensure_workspace, normalize_workspace_root
from litehive.config.workspace_files import workspace_dir
from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow
from litehive.domain.task import TaskRecord
from litehive.state.lock_manager import WorkspaceLockManager
from litehive.state.persist import load_state, set_pool_stop_reason
from litehive.state.records import list_tasks
from litehive.tasks.paths import tasks_root

DETECTABLE_ATTENTION_KINDS = {
    "duplicate_task_id",
    "flagged_task",
    "human_checkpoint_before_commit",
    "merge_failed_task",
    "origin_divergence",
    "stale_worktree",
    "stale_worktree_metadata",
}

ATTENTION_PRIORITIES = {
    "origin_divergence": 0,
    "destructive_git_denied": 1,
    "human_checkpoint_before_commit": 2,
    "merge_failed_task": 3,
    "duplicate_task_id": 4,
    "stale_worktree": 5,
    "stale_worktree_metadata": 5,
    "flagged_task": 6,
}

_ATTENTION_ID_WIDTH = 6
_MIGRATION_MARKER = ".migration-complete"
_TASK_WORKTREE_NAME_RE = re.compile(r"^T-\d{4}-")


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
    root = normalize_workspace_root(workspace, source="append_attention_log")
    path = workspace_dir(root) / "runtime" / "attention.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utcnow()}\t{message}\n")


def attention_priority(item: AttentionItem) -> tuple[int, str, int]:
    return (ATTENTION_PRIORITIES.get(item.kind, 100), item.created_at, item.id or 0)


class AttentionStore:
    def __init__(self, root: Path) -> None:
        self.root = normalize_workspace_root(root, source="attention_store")

    def list_items(self, *, include_resolved: bool = False) -> list[AttentionItem]:
        with self._store_lock():
            self._ensure_store_ready_locked()
            items = self._load_items_locked()
        if include_resolved:
            return items
        return [item for item in items if item.status == "pending"]

    def create_or_keep(self, item: AttentionItem) -> AttentionItem:
        with self._store_lock():
            self._ensure_store_ready_locked()
            items = self._load_items_locked()
            stored = self._create_or_keep_locked(items, item)
        return stored

    @staticmethod
    def _refreshed_pending_item(existing: AttentionItem, replacement: AttentionItem) -> AttentionItem | None:
        updated = existing.model_copy(
            update={
                "task_id": replacement.task_id,
                "kind": replacement.kind,
                "title": replacement.title,
                "reason": replacement.reason,
                "suggested_action": replacement.suggested_action,
                "metadata": replacement.metadata,
            }
        )
        if updated == existing:
            return None
        return updated

    def resolve(self, item_id: int, *, resolution: str) -> AttentionItem | None:
        with self._store_lock():
            self._ensure_store_ready_locked()
            item = self._read_item_locked(item_id)
            if item is None:
                return None
            if item.status == "resolved":
                return item
            item.status = "resolved"
            item.resolved_at = utcnow()
            item.resolution = resolution
            if resolution == "resolved by operator" and item.kind in DETECTABLE_ATTENTION_KINDS:
                item.metadata["suppressed_until_cleared"] = True
                item.metadata["condition_cleared_after_resolution"] = False
            self._write_item_locked(item)
            return item

    def reconcile(self, detected: list[AttentionItem]) -> list[AttentionItem]:
        with self._store_lock():
            self._ensure_store_ready_locked()
            all_items = self._load_items_locked()
            latest_by_key: dict[str, AttentionItem] = {}
            pending_by_key: dict[str, AttentionItem] = {}
            for item in all_items:
                latest_by_key[item.dedupe_key] = item
                if item.status == "pending":
                    pending_by_key[item.dedupe_key] = item
            detected_keys = {item.dedupe_key for item in detected}

            for item in detected:
                previous = latest_by_key.get(item.dedupe_key)
                if previous is not None and self._is_operator_suppressed(previous):
                    continue
                stored = self._create_or_keep_locked(all_items, item, pending_by_key=pending_by_key)
                latest_by_key[stored.dedupe_key] = stored
                if stored.status == "pending":
                    pending_by_key[stored.dedupe_key] = stored

            for item in all_items:
                if item.kind not in DETECTABLE_ATTENTION_KINDS:
                    continue
                if item.dedupe_key in detected_keys:
                    continue
                if item.status == "pending":
                    item.status = "resolved"
                    item.resolved_at = utcnow()
                    item.resolution = "auto-resolved by attention reconciliation"
                    self._write_item_locked(item)
                    continue
                if self._is_operator_suppressed(item):
                    item.metadata["condition_cleared_after_resolution"] = True
                    self._write_item_locked(item)

            return [item for item in all_items if item.status == "pending"]

    def _create_or_keep_locked(
        self,
        all_items: list[AttentionItem],
        item: AttentionItem,
        *,
        pending_by_key: dict[str, AttentionItem] | None = None,
    ) -> AttentionItem:
        pending_lookup = (
            pending_by_key
            if pending_by_key is not None
            else {candidate.dedupe_key: candidate for candidate in all_items if candidate.status == "pending"}
        )
        existing_item = pending_lookup.get(item.dedupe_key)
        if existing_item is not None:
            refreshed = self._refreshed_pending_item(existing_item, item)
            if refreshed is not None:
                self._replace_loaded_item(all_items, refreshed)
                pending_lookup[refreshed.dedupe_key] = refreshed
                self._write_item_locked(refreshed)
                return refreshed
            return existing_item

        stored = item.model_copy(deep=True)
        stored.id = self._next_id(all_items)
        all_items.append(stored)
        if stored.status == "pending":
            pending_lookup[stored.dedupe_key] = stored
        self._write_item_locked(stored)
        return stored

    @staticmethod
    def _replace_loaded_item(all_items: list[AttentionItem], replacement: AttentionItem) -> None:
        for index, current in enumerate(all_items):
            if current.id == replacement.id:
                all_items[index] = replacement
                return
        all_items.append(replacement)

    @staticmethod
    def _is_operator_suppressed(item: AttentionItem) -> bool:
        return (
            item.status == "resolved"
            and item.resolution == "resolved by operator"
            and item.metadata.get("suppressed_until_cleared") is True
            and item.metadata.get("condition_cleared_after_resolution") is not True
        )

    def _load_items_locked(self) -> list[AttentionItem]:
        try:
            with connect_workspace_db(self.root) as connection:
                rows = connection.execute(
                    "SELECT id, task_id, created_at, kind, payload FROM attention ORDER BY id ASC"
                ).fetchall()
        except sqlite3.Error:
            return []
        items: list[AttentionItem] = []
        for row in rows:
            try:
                items.append(self._row_to_item(row))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return items

    def _read_item_locked(self, item_id: int) -> AttentionItem | None:
        try:
            with connect_workspace_db(self.root) as connection:
                row = connection.execute(
                    "SELECT id, task_id, created_at, kind, payload FROM attention WHERE id = ?",
                    (item_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return self._row_to_item(row)

    def _read_item_path_locked(self, path: Path) -> AttentionItem | None:
        if not path.exists():
            return None
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError:
            return None
        if not isinstance(payload, dict):
            raise ValueError(f"attention item at {path} is not a YAML mapping")
        item_id = int(path.stem)
        payload["id"] = item_id
        return AttentionItem(**payload)

    def _write_item_locked(self, item: AttentionItem) -> None:
        if item.id is None:
            raise ValueError("attention item id is required before writing")
        payload = item.model_dump(mode="json")
        with connect_workspace_db(self.root) as connection:
            connection.execute(
                """
                INSERT INTO attention (id, task_id, created_at, kind, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    task_id = excluded.task_id,
                    created_at = excluded.created_at,
                    kind = excluded.kind,
                    payload = excluded.payload
                """,
                (
                    item.id,
                    item.task_id,
                    item.created_at,
                    item.kind,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            connection.commit()

    def _ensure_store_ready_locked(self) -> None:
        items_dir = self._items_dir()
        items_dir.mkdir(parents=True, exist_ok=True)
        marker_path = items_dir / _MIGRATION_MARKER
        legacy_paths = self._item_paths_locked()
        if marker_path.exists() and not legacy_paths:
            return
        for legacy_item in self._legacy_file_items():
            self._write_item_locked(legacy_item)
        for path in legacy_paths:
            path.unlink(missing_ok=True)
        marker_path.write_text(f"completed_at={utcnow()}\n", encoding="utf-8")

    def _legacy_file_items(self) -> list[AttentionItem]:
        items: list[AttentionItem] = []
        for path in self._item_paths_locked():
            item = self._read_item_path_locked(path)
            if item is not None:
                items.append(item)
        return items

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> AttentionItem:
        payload = json.loads(row["payload"])
        payload.setdefault("created_at", row["created_at"])
        payload.setdefault("kind", row["kind"])
        payload.setdefault("task_id", row["task_id"])
        payload["id"] = int(row["id"])
        return AttentionItem(**payload)

    def _item_paths_locked(self) -> list[Path]:
        items_dir = self._items_dir()
        if not items_dir.exists():
            return []
        candidates = [
            path
            for path in items_dir.iterdir()
            if path.is_file() and path.suffix == ".yaml" and path.stem.isdigit()
        ]
        return sorted(candidates, key=lambda path: int(path.stem))

    def _next_id(self, all_items: list[AttentionItem]) -> int:
        return max((item.id or 0 for item in all_items), default=0) + 1

    def _items_dir(self) -> Path:
        return workspace_dir(self.root) / "attention"

    def _item_path(self, item_id: int) -> Path:
        return self._items_dir() / f"{item_id:0{_ATTENTION_ID_WIDTH}d}.yaml"

    @contextmanager
    def _store_lock(self):
        manager = WorkspaceLockManager(self._items_dir() / ".lock", pid_is_alive=lambda pid: False)
        handle = manager.open()
        manager.lock(handle, nonblocking=False)
        try:
            yield
        finally:
            manager.unlock(handle)
            handle.close()


def attention_store(root: Path) -> AttentionStore:
    return AttentionStore(root)


def _existing_workspace_root(root: Path, *, source: str) -> Path:
    resolved = normalize_workspace_root(root, source=source)
    if not workspace_dir(resolved).is_dir():
        raise ValueError(f"workspace root does not exist: {resolved}")
    return resolved


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
    root = normalize_workspace_root(root, source="record_attention")
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


def list_attention(
    root: Path,
    *,
    reconcile: bool = True,
    auto_resolve: bool = True,
) -> list[AttentionItem]:
    root = _existing_workspace_root(root, source="list_attention")
    if reconcile:
        return reconcile_attention(root, auto_resolve=auto_resolve)
    return sorted(attention_store(root).list_items(), key=attention_priority)


def resolve_attention(root: Path, item_id: int) -> AttentionItem | None:
    root = _existing_workspace_root(root, source="resolve_attention")
    return attention_store(root).resolve(item_id, resolution="resolved by operator")


def reconcile_attention(root: Path, *, auto_resolve: bool = True) -> list[AttentionItem]:
    root = _existing_workspace_root(root, source="reconcile_attention")
    state = load_state(root, bootstrap=False)
    _import_attention_log_events(root)
    detected = _detect_attention_items(root, state.pool_stop_reason)
    if auto_resolve:
        _auto_resolve_stale_worktree_metadata_items(root)
    unresolved = attention_store(root).reconcile(detected)
    if state.pool_stop_reason == "attention_required" and not unresolved:
        set_pool_stop_reason(root, None)
    return sorted(unresolved, key=attention_priority)


def waiting_for_you_lines(root: Path, *, limit: int = 5) -> list[str]:
    try:
        items = list_attention(root, auto_resolve=False)
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
    tasks = list_tasks(root, strict=False)
    state = load_state(root, bootstrap=False)
    detected: list[AttentionItem] = []
    detected.extend(_duplicate_id_items(root))
    detected.extend(_flagged_and_merge_failed_items(root, tasks))
    detected.extend(_stale_worktree_items(root, tasks, state))
    detected.extend(_stale_worktree_metadata_items(root, tasks))
    divergence = _origin_divergence_item(root)
    if divergence is not None:
        detected.append(divergence)
    checkpoint = _human_checkpoint_item(tasks, state.active_task_id, pool_stop_reason)
    if checkpoint is not None:
        detected.append(checkpoint)
    return detected


def _import_attention_log_events(root: Path) -> None:
    log_path = workspace_dir(root) / "runtime" / "attention.log"
    if not log_path.exists():
        return
    seen_keys = {item.dedupe_key for item in attention_store(root).list_items(include_resolved=True)}
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
        match = re.match(r"^(T-\d{4})-", child.name)
        if match is None:
            continue
        task_id = match.group(1)
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


def _flagged_and_merge_failed_items(root: Path, tasks: list[TaskRecord]) -> list[AttentionItem]:
    items: list[AttentionItem] = []
    for task in tasks:
        is_merge_failed_task = task.status == "flagged" and task.flag_reason == "merge_failed"
        if task.status == "flagged" and not is_merge_failed_task:
            items.append(
                AttentionItem(
                    task_id=task.id,
                    kind="flagged_task",
                    title=f"Task {task.id} is flagged",
                    reason=(
                        f"Task is paused in `{task.pipeline_status}` and requires operator review."
                        f" Flag reason: {task.flag_reason or 'unknown'}."
                    ),
                    suggested_action=(
                        f"Run `litehive task debug {task.id}` and then `litehive queue promote {task.id}`"
                        " when it is ready to continue."
                    ),
                    dedupe_key=f"flagged_task:{task.id}",
                    metadata={"pipeline_status": task.pipeline_status, "flag_reason": task.flag_reason},
                )
            )
        if is_merge_failed_task:
            title = f"Task {task.id} needs merge recovery"
            reason = "Checkpoint commit or merge resolution failed and the managed worktree needs operator recovery."
            metadata: dict[str, Any] = {"pipeline_status": task.pipeline_status, "flag_reason": task.flag_reason}
            try:
                from litehive.lifecycle.persistence import SqlitePersistence

                state = SqlitePersistence(root).load(task.id)
                trigger = state.active_recovery_trigger
                if trigger is None and state.recovery_history:
                    trigger = state.recovery_history[-1].trigger
                origin_stage = None if trigger is None else trigger.origin_stage
                failed_reason = (
                    state.failed_reason.value if hasattr(state.failed_reason, "value") else state.failed_reason
                )
                metadata["failed_reason"] = failed_reason
                metadata["origin_stage"] = origin_stage
                if origin_stage != "merge_resolving" and failed_reason == "recovery_crashed":
                    title = f"Task {task.id} needs recovery follow-up"
                    reason = "Recovery crashed while handling commit-stage failure; operator follow-up is required."
            except Exception:
                pass
            items.append(
                AttentionItem(
                    task_id=task.id,
                    kind="merge_failed_task",
                    title=title,
                    reason=reason,
                    suggested_action=(
                        f"Run `litehive task debug {task.id} --worktree` and then "
                        f"`litehive queue requeue {task.id}`."
                    ),
                    dedupe_key=f"merge_failed_task:{task.id}",
                    metadata=metadata,
                )
            )
    return items


def _stale_worktree_items(root: Path, tasks: list[TaskRecord], state) -> list[AttentionItem]:
    from litehive.config.paths import workspace_path
    from litehive.tasks.queue import is_task_eligible_for_execution
    from litehive.worktree import (
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
    worktrees_root = workspace_path(root, "worktrees")
    if worktrees_root.exists():
        for child in sorted(worktrees_root.iterdir()):
            if not child.is_dir():
                continue
            if _TASK_WORKTREE_NAME_RE.match(child.name) is None:
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
        f"Run `litehive task debug {task_id} --worktree` to inspect the task, then continue with `litehive run`"
        " when you are ready to commit."
        if task_id
        else "Inspect the active task and then continue with `litehive run` when you are ready to commit."
    )
    return AttentionItem(
        task_id=task_id,
        kind="human_checkpoint_before_commit",
        title=title,
        reason=reason,
        suggested_action=action,
        dedupe_key=f"human_checkpoint_before_commit:{task_id or 'workspace'}",
    )


def _stale_worktree_metadata_items(root: Path, tasks: list[TaskRecord]) -> list[AttentionItem]:
    del tasks
    store = attention_store(root)
    existing_items = store.list_items(include_resolved=True)
    return [
        item for item in existing_items if item.kind == "stale_worktree_metadata" and item.status == "pending"
    ]


def _auto_resolve_stale_worktree_metadata_items(root: Path) -> None:
    from litehive.domain.task_ops import WorkspaceConflictError
    from litehive.state.locking import runner_lock_is_active
    from litehive.state.records import clear_task_worktree_path, get_task, save_task

    if runner_lock_is_active(root):
        return

    store = attention_store(root)
    existing_items = store.list_items(include_resolved=True)
    deferred_items = [item for item in existing_items if item.kind == "stale_worktree_metadata" and item.status == "pending"]

    for item in deferred_items:
        if item.task_id is None:
            continue

        try:
            task = get_task(root, item.task_id)
            if task is not None and task.runtime.git.worktree_path is not None:
                clear_task_worktree_path(task)
                save_task(root, task)
                store.resolve(item.id or 0, resolution="auto-resolved: worktree metadata cleared")
        except WorkspaceConflictError:
            continue
        except Exception:
            store.resolve(item.id or 0, resolution="auto-resolved: task no longer exists or clearing not needed")


def _default_dedupe_key(kind: str, *, task_id: str | None, title: str, reason: str) -> str:
    if task_id:
        return f"{kind}:{task_id}"
    return f"{kind}:{title}:{reason}"
