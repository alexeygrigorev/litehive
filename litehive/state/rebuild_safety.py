"""Safety checks for destructive workspace database rebuilds."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from litehive.workspace import Workspace

TASK_EVENT_LOG_NAME = "task-events.jsonl"
_TASK_DIR_RE = re.compile(r"^(T-\d{4})(?:-|$)")
_TASK_PAYLOAD_KEYS = {"task_intent", "task_state"}


class RebuildSafetyError(RuntimeError):
    """
    Raised when a rebuild would drop task rows that still have evidence.

    The rebuild path raises this instead of silently losing data so an
    operator can decide whether to repair the replay source first or
    pass an explicit override.
    """


@dataclass(frozen=True, slots=True)
class RebuildSafetyReport:
    """
    Diagnostic snapshot returned by ``assert_database_rebuild_safe``.

    Captures the three id sets the safety check compares (sqlite, on-disk
    artifacts, replay) plus the missing-from-replay tuple, so the operator
    CLI can render a useful "what's about to happen?" preview before the
    destructive rebuild step runs.
    """

    sqlite_task_ids: frozenset[str]
    task_dir_ids: frozenset[str]
    replay_task_ids: frozenset[str]
    missing_task_ids: tuple[str, ...]


def sqlite_task_ids(db_path: Path) -> set[str]:
    """
    Enumerate task ids the live SQLite database still owns.

    The rebuild-safety check compares this set against what the replay
    source can reconstruct; a corrupt DB is treated as "no rows" rather
    than blocking rebuild forever, since the whole point of rebuild is
    to recover from a bad DB.
    """
    if not db_path.exists():
        return set()
    try:
        with sqlite3.connect(db_path) as connection:
            table_rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            tables = {str(row[0]) for row in table_rows}
            task_ids: set[str] = set()
            for table_name in ("task_state", "task_intent"):
                if table_name not in tables:
                    continue
                rows = connection.execute(f"SELECT task_id FROM {table_name}").fetchall()
                task_ids.update(str(row[0]) for row in rows if row[0] is not None)
            return task_ids
    except sqlite3.DatabaseError:
        return set()


class DatabaseRebuildSafety:
    """
    Workspace-bound safety policy for destructive DB rebuilds.

    Owns artifact/replay coverage checks and the pre-rebuild backup for
    one workspace. `sqlite_task_ids` stays a pure path helper because it
    only inspects a supplied DB path.
    """

    def __init__(self, workspace: "Workspace") -> None:
        self.workspace = workspace

    def task_artifact_dir_ids(self) -> set[str]:
        """
        Discover task ids that have on-disk artifact directories.
        """
        tasks_dir = self.workspace.control_dir() / "tasks"
        if not tasks_dir.exists():
            return set()

        task_ids: set[str] = set()
        try:
            children = list(tasks_dir.iterdir())
        except OSError:
            return task_ids
        for child in children:
            if not child.is_dir():
                continue
            match = _TASK_DIR_RE.match(child.name)
            if match is not None:
                task_ids.add(match.group(1))
        return task_ids

    def event_log_replay_task_ids(self) -> set[str]:
        """
        Compute which task ids the append-only event log can reconstruct.
        """
        path = self.workspace.runtime_path(TASK_EVENT_LOG_NAME)
        if not path.exists():
            return set()

        task_ids: set[str] = set()
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            task_id = event.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            event_type = str(event.get("event_type") or "")
            if event_type in {"task_deleted", "task_removed"}:
                task_ids.discard(task_id)
                continue
            payload = event.get("payload")
            if isinstance(payload, dict) and any(key in payload for key in _TASK_PAYLOAD_KEYS):
                task_ids.add(task_id)
        return task_ids

    def assert_safe(
        self,
        db_path: Path,
        *,
        replay_task_ids: Iterable[str] | None = None,
        operation: str,
    ) -> RebuildSafetyReport:
        """
        Refuse a destructive DB rebuild that would drop task rows.
        """
        sqlite_ids = sqlite_task_ids(db_path)
        artifact_ids = self.task_artifact_dir_ids()
        if replay_task_ids is None:
            replay_source = self.event_log_replay_task_ids()
        else:
            replay_source = replay_task_ids
        replay_ids = set(replay_source)

        expected_ids = set(sqlite_ids)
        missing_ids = tuple(sorted(expected_ids - replay_ids))
        report = RebuildSafetyReport(
            sqlite_task_ids=frozenset(sqlite_ids),
            task_dir_ids=frozenset(artifact_ids),
            replay_task_ids=frozenset(replay_ids),
            missing_task_ids=missing_ids,
        )
        if missing_ids:
            sample = ", ".join(missing_ids[:10])
            if len(missing_ids) <= 10:
                suffix = ""
            else:
                suffix = f", ... ({len(missing_ids)} total)"
            raise RebuildSafetyError(
                f"refusing {operation}: replay source covers {len(replay_ids)} task(s), "
                f"but existing DB task rows reference {len(expected_ids)} task(s); "
                f"missing from replay: {sample}{suffix}"
            )
        return report

    def backup_before_rebuild(self, db_path: Path, label: str) -> Path | None:
        """
        Snapshot the current DB into ``backups/`` before a rebuild touches it.
        """
        if not db_path.exists():
            return None
        backup_dir = self.workspace.runtime_path("backups")
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "pre-rebuild"
        destination = backup_dir / f"data-{timestamp}-{safe_label}.db"

        try:
            source = sqlite3.connect(db_path)
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
                target.commit()
            finally:
                target.close()
                source.close()
        except sqlite3.DatabaseError:
            shutil.copy2(db_path, destination)
        return destination
