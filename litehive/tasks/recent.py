"""Recent task summary queries backed directly by the workspace SQLite DB."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re

import yaml

from litehive.db.schema import connect_workspace_db
from litehive.tasks.paths import tasks_root


_COMPACT_DURATION_RE = re.compile(r"^(\d+)([dhms])$", re.IGNORECASE)
_DURATION_MULTIPLIERS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


@dataclass(frozen=True)
class RecentTaskSummary:
    task_id: str
    title: str
    transition_count: int
    elapsed_seconds: int
    final_stage: str
    status: str


def parse_compact_duration_seconds(duration_str: str) -> int:
    match = _COMPACT_DURATION_RE.match(duration_str.strip())
    if match is None:
        raise ValueError(
            f"Invalid duration format '{duration_str}'. Use <number><unit> where unit is d/h/m/s (e.g. 24h)"
        )
    value = int(match.group(1))
    unit = match.group(2).lower()
    return value * _DURATION_MULTIPLIERS[unit]


def list_recent_task_summaries(
    root: Path,
    *,
    since: str = "24h",
    now: datetime | None = None,
) -> list[RecentTaskSummary]:
    window_seconds = parse_compact_duration_seconds(since)
    cutoff = _coerce_utc(now) - timedelta(seconds=window_seconds)
    rows = _load_recent_summary_rows(root, cutoff=cutoff.replace(microsecond=0).isoformat())
    titles = _load_task_titles(root, [str(row["task_id"]) for row in rows])
    archived_statuses = _load_archived_task_statuses(root, [str(row["task_id"]) for row in rows])
    return [
        RecentTaskSummary(
            task_id=str(row["task_id"]),
            title=titles.get(str(row["task_id"]), "-"),
            transition_count=int(row["transition_count"]),
            elapsed_seconds=max(0, int(row["elapsed_seconds"])),
            final_stage=str(row["final_stage"] or "-"),
            status=archived_statuses.get(str(row["task_id"]), str(row["status"] or "-")),
        )
        for row in rows
    ]


def format_elapsed_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{remaining_seconds:02d}s"
    hours, remaining_minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{remaining_minutes:02d}m"
    days, remaining_hours = divmod(hours, 24)
    return f"{days}d{remaining_hours:02d}h"


def _coerce_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC).replace(microsecond=0)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC).replace(microsecond=0)
    return now.astimezone(UTC).replace(microsecond=0)


def _load_recent_summary_rows(root: Path, *, cutoff: str):
    # Keep the hot path in SQLite: summarize recent transitions in one query,
    # then hydrate titles from persisted task records afterward.
    with connect_workspace_db(root) as connection:
        return connection.execute(
            """
            WITH recent AS (
                SELECT
                    task_id,
                    COUNT(*) AS transition_count,
                    CAST(ROUND((julianday(MAX(created_at)) - julianday(MIN(created_at))) * 86400.0) AS INTEGER)
                        AS elapsed_seconds,
                    MAX(created_at) AS last_touched_at,
                    MAX(seq) AS last_seq
                FROM pipeline_transitions
                WHERE julianday(created_at) >= julianday(?)
                GROUP BY task_id
            )
            SELECT
                recent.task_id,
                recent.transition_count,
                recent.elapsed_seconds,
                COALESCE(last_transition.to_stage, json_extract(state.payload, '$.pipeline_status'), '-') AS final_stage,
                COALESCE(json_extract(state.payload, '$.status'), '-') AS status
            FROM recent
            LEFT JOIN pipeline_transitions AS last_transition
                ON last_transition.task_id = recent.task_id
                AND last_transition.seq = recent.last_seq
            LEFT JOIN task_state AS state
                ON state.task_id = recent.task_id
            ORDER BY julianday(recent.last_touched_at) DESC, recent.task_id ASC
            """,
            (cutoff,),
        ).fetchall()


def _load_task_titles(root: Path, task_ids: list[str]) -> dict[str, str]:
    titles: dict[str, str] = {}
    active_root = tasks_root(root, bootstrap=False)
    archive_root = active_root / "archive"
    for task_id in task_ids:
        for base in (active_root, archive_root):
            task_record = _find_task_record_path(base, task_id)
            if task_record is None:
                continue
            title = _read_task_title(task_record)
            if title is not None:
                titles[task_id] = title
                break
    return titles


def _load_archived_task_statuses(root: Path, task_ids: list[str]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    archive_root = tasks_root(root, bootstrap=False) / "archive"
    for task_id in task_ids:
        task_record = _find_task_record_path(archive_root, task_id)
        if task_record is None:
            continue
        status = _read_task_status(task_record)
        if status is not None:
            statuses[task_id] = status
    return statuses


def _find_task_record_path(base: Path, task_id: str) -> Path | None:
    if not base.exists():
        return None
    matches = sorted(base.glob(f"{task_id}-*/task.yaml"))
    return matches[0] if matches else None


def _read_task_title(path: Path) -> str | None:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(payload, dict):
        return None
    title = payload.get("title")
    return title.strip() if isinstance(title, str) and title.strip() else None


def _read_task_status(path: Path) -> str | None:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip()
    return "archived"
