<<<<<<< HEAD
"""SQLite-backed storage for structured subagent session artifacts."""

import json
from pathlib import Path
from typing import Any

from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow


_UNSET = object()


def _load_subagent_payload(root: Path, task_id: str, subagent_id: str) -> tuple[dict[str, Any], str | None]:
    with connect_workspace_db(root) as connection:
        row = connection.execute(
            """
            SELECT created_at, payload
            FROM subagent_sessions
            WHERE task_id = ? AND subagent_id = ?
            """,
            (task_id, subagent_id),
        ).fetchone()
    if row is None:
        return {}, None
    payload = json.loads(row["payload"])
    return payload if isinstance(payload, dict) else {}, row["created_at"]


def load_subagent_artifacts(root: Path, task_id: str, subagent_id: str) -> dict[str, Any]:
    payload, _ = _load_subagent_payload(root, task_id, subagent_id)
    return payload
=======
"""Compatibility helpers for reading persisted subagent artifacts."""

from __future__ import annotations

import gzip
from pathlib import Path

import yaml

from litehive.state.persist import write_atomic_files


def _subagent_base(root: Path, task_id: str, subagent_id: str) -> Path | None:
    task_root = root / ".litehive" / "tasks"
    task_dirs = sorted(task_root.glob(f"{task_id}-*"))
    for task_dir in task_dirs:
        exact = task_dir / "subagents" / subagent_id
        if exact.exists():
            return exact
    matches = sorted(task_root.glob(f"{task_id}-*/subagents/{subagent_id}-*"))
    if matches:
        return matches[0]
    return None


def _read_yaml(path: Path) -> object:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_subagent_artifacts(root: Path, task_id: str, subagent_id: str) -> dict[str, object]:
    base = _subagent_base(root, task_id, subagent_id)
    if base is None:
        return {}
    artifacts: dict[str, object] = {}
    for name, candidates in {
        "session": [base / "session.yaml"],
        "report": [base / "report.yaml"],
        "timeline": [base / "timeline.yaml", base / "timeline.yaml.gz"],
    }.items():
        for candidate in candidates:
            if not candidate.exists():
                continue
            artifacts[name] = _read_yaml(candidate)
            break
    return artifacts


def load_subagent_session(root: Path, task_id: str, subagent_id: str) -> dict[str, object]:
    session = load_subagent_artifacts(root, task_id, subagent_id).get("session")
    return session if isinstance(session, dict) else {}


def load_subagent_report(root: Path, task_id: str, subagent_id: str) -> dict[str, object]:
    report = load_subagent_artifacts(root, task_id, subagent_id).get("report")
    return report if isinstance(report, dict) else {}


def load_subagent_timeline(root: Path, task_id: str, subagent_id: str) -> dict[str, object]:
    timeline = load_subagent_artifacts(root, task_id, subagent_id).get("timeline")
    return timeline if isinstance(timeline, dict) else {}
>>>>>>> 61b0a5fa (litehive T-0398: auto-commit worktree changes)


def save_subagent_artifacts(
    root: Path,
    task_id: str,
    subagent_id: str,
    *,
<<<<<<< HEAD
    session: dict[str, Any] | object = _UNSET,
    report: dict[str, Any] | object = _UNSET,
    timeline: dict[str, Any] | None | object = _UNSET,
) -> None:
    payload, created_at = _load_subagent_payload(root, task_id, subagent_id)
    if session is not _UNSET:
        payload["session"] = session
    if report is not _UNSET:
        payload["report"] = report
    if timeline is not _UNSET:
        if timeline is None:
            payload.pop("timeline", None)
        else:
            payload["timeline"] = timeline
    now = utcnow()
    created_at = created_at or now
    with connect_workspace_db(root) as connection:
        connection.execute(
            """
            INSERT INTO subagent_sessions (
                task_id,
                subagent_id,
                created_at,
                updated_at,
                payload
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id, subagent_id) DO UPDATE SET
                updated_at = excluded.updated_at,
                payload = excluded.payload
            """,
            (
                task_id,
                subagent_id,
                created_at,
                now,
                json.dumps(payload, default=str, sort_keys=True),
            ),
        )


def load_subagent_session(root: Path, task_id: str, subagent_id: str) -> dict[str, Any]:
    payload = load_subagent_artifacts(root, task_id, subagent_id)
    session = payload.get("session")
    return session if isinstance(session, dict) else {}


def load_subagent_report(root: Path, task_id: str, subagent_id: str) -> dict[str, Any]:
    payload = load_subagent_artifacts(root, task_id, subagent_id)
    report = payload.get("report")
    return report if isinstance(report, dict) else {}


def load_subagent_timeline(root: Path, task_id: str, subagent_id: str) -> dict[str, Any]:
    payload = load_subagent_artifacts(root, task_id, subagent_id)
    timeline = payload.get("timeline")
    return timeline if isinstance(timeline, dict) else {}
=======
    session: dict[str, object] | None = None,
    report: dict[str, object] | None = None,
    timeline: dict[str, object] | None = None,
) -> None:
    base = _subagent_base(root, task_id, subagent_id)
    if base is None:
        task_root = root / ".litehive" / "tasks"
        matches = sorted(task_root.glob(f"{task_id}-*"))
        if not matches:
            raise ValueError(f"Task {task_id} not found")
        base = matches[0] / "subagents" / f"{subagent_id}"
        base.mkdir(parents=True, exist_ok=True)

    writes: dict[Path, str] = {}
    if session is not None:
        writes[base / "session.yaml"] = yaml.safe_dump(session, sort_keys=False)
    if report is not None:
        writes[base / "report.yaml"] = yaml.safe_dump(report, sort_keys=False)
    if timeline is not None:
        writes[base / "timeline.yaml"] = yaml.safe_dump(timeline, sort_keys=False)
    if writes:
        write_atomic_files(writes)
>>>>>>> 61b0a5fa (litehive T-0398: auto-commit worktree changes)
