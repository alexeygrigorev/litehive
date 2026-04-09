from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader

_POLL_INTERVAL_MS = 1500
_MAX_ARTIFACT_BYTES = 64 * 1024
_RUN_ALL_PREVIEW_BYTES = 32 * 1024
_RUN_ALL_LOG_LIMIT = 8
_SESSION_LIMIT = 12
_STREAM_KEEPALIVE_SECONDS = 15.0
_STREAM_RETRY_MS = 2000
_STREAM_SCAN_INTERVAL_SECONDS = 0.25
_WEB_REVIEWABLE_STAGES = {"testing", "accepting"}
_WEB_VERDICT_OPTIONS = ("pass", "reject", "comment")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=False)


def _render_index() -> str:
    template = _jinja_env.get_template("index.html")
    return template.render(
        poll_interval_ms=_POLL_INTERVAL_MS,
        poll_seconds=f"{_POLL_INTERVAL_MS / 1000:.1f}",
    )


@dataclass(slots=True)
class SessionArtifact:
    kind: str
    label: str
    path: str
    source: str
    content: str
    size: int
    truncated: bool
    available: bool


def _read_text_file(path: Path | None, *, max_bytes: int) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"content": "", "size": 0, "truncated": False, "available": False}
    if path.suffix == ".gz":
        raw = gzip.decompress(path.read_bytes())
        size = len(raw)
        if size > max_bytes:
            raw = raw[-max_bytes:]
            truncated = True
        else:
            truncated = False
        return {
            "content": raw.decode("utf-8", errors="replace"),
            "size": size,
            "truncated": truncated,
            "available": True,
        }
    raw = path.read_bytes()
    size = len(raw)
    if size > max_bytes:
        raw = raw[-max_bytes:]
        truncated = True
    else:
        truncated = False
    return {
        "content": raw.decode("utf-8", errors="replace"),
        "size": size,
        "truncated": truncated,
        "available": True,
    }


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _relative_to_root(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def _mtime(path: Path) -> str | None:
    try:
        return _read_iso_now(path.stat().st_mtime)
    except FileNotFoundError:
        return None


def _read_iso_now(timestamp: float | None = None) -> str:
    from datetime import UTC, datetime

    if timestamp is None:
        return datetime.now(UTC).replace(microsecond=0).isoformat()
    return datetime.fromtimestamp(timestamp, UTC).replace(microsecond=0).isoformat()


def _resolve_artifact_path(base: Path, name: str) -> Path | None:
    direct = base / name
    if direct.exists():
        return direct
    gzip_path = base / f"{name}.gz"
    if gzip_path.exists():
        return gzip_path
    return None


def _stable_signature(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _workspace_snapshot_diff(
    previous: dict[str, Any] | None, current: dict[str, Any]
) -> dict[str, Any]:
    previous_task_ids = {item["id"] for item in previous["tasks"]} if previous else set()
    current_task_ids = {item["id"] for item in current["tasks"]}
    previous_by_id = {item["id"]: item for item in previous["tasks"]} if previous else {}
    changed_tasks = sorted(
        task_id
        for task_id, task in {item["id"]: item for item in current["tasks"]}.items()
        if previous_by_id.get(task_id) != task
    )
    return {
        "active_task_id": current.get("active_task_id"),
        "runner_changed": None
        if previous is None
        else previous.get("runner") != current.get("runner"),
        "queue_changed": None
        if previous is None
        else previous.get("queue") != current.get("queue"),
        "added_task_ids": sorted(current_task_ids - previous_task_ids),
        "removed_task_ids": sorted(previous_task_ids - current_task_ids),
        "changed_task_ids": changed_tasks,
    }


def _session_payload_diff(
    previous: dict[str, Any] | None, current: dict[str, Any] | None
) -> dict[str, Any]:
    if previous is None and current is None:
        return {"status": "missing", "changed_artifacts": []}
    if previous is None:
        return {
            "status": "created",
            "changed_artifacts": [artifact["kind"] for artifact in current.get("artifacts", [])],
        }
    if current is None:
        return {"status": "removed", "changed_artifacts": []}
    previous_artifacts = {artifact["kind"]: artifact for artifact in previous.get("artifacts", [])}
    changed_artifacts = [
        artifact["kind"]
        for artifact in current.get("artifacts", [])
        if previous_artifacts.get(artifact["kind"]) != artifact
    ]
    status = "changed" if previous != current else "unchanged"
    return {"status": status, "changed_artifacts": changed_artifacts}


def _iter_stream_paths(root: Path) -> list[Path]:
    candidates: list[Path] = []
    state_path = root / ".litehive" / "state.yaml"
    if state_path.exists():
        candidates.append(state_path)
    config_file = root / ".litehive" / "config.yaml"
    if config_file.exists():
        candidates.append(config_file)
    engine_monitoring = root / ".litehive" / "engine-monitoring.yaml"
    if engine_monitoring.exists():
        candidates.append(engine_monitoring)
    task_root = root / ".litehive" / "tasks"
    if task_root.exists():
        for task_path in sorted(path for path in task_root.iterdir() if path.is_dir()):
            for name in ("task.yaml", "runtime.yaml", "thread.yaml", "events.jsonl"):
                candidate = task_path / name
                if candidate.exists():
                    candidates.append(candidate)
            candidates.extend(sorted((task_path / "reports").glob("*.yaml")))
            candidates.extend(sorted((task_path / "recovery").glob("*.yaml")))
            candidates.extend(sorted((task_path / "subagents").glob("*/session.yaml")))
            for artifact in (
                "transcript.md",
                "transcript.md.gz",
                "stdout.log",
                "stdout.txt",
                "stdout.txt.gz",
                "stderr.log",
                "stderr.txt",
                "stderr.txt.gz",
            ):
                candidates.extend(sorted((task_path / "subagents").glob(f"*/{artifact}")))
    run_all_root = root / ".litehive" / "logs" / "run-all"
    if run_all_root.exists():
        for directory in sorted(path for path in run_all_root.iterdir() if path.is_dir()):
            candidates.append(directory)
            candidates.extend(sorted(path for path in directory.iterdir() if path.is_file()))
    return candidates


def _serialize_engine_record(record: dict[str, Any]) -> dict[str, Any]:
    usage = record.get("usage")
    metadata = record.get("metadata") or {}
    token_cost_keys = [
        key
        for key in sorted(metadata)
        if any(marker in key.lower() for marker in ("token", "cost", "credit", "budget"))
    ]
    return {
        **record,
        "usage": usage if isinstance(usage, dict) else None,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "token_cost_fields": {key: metadata[key] for key in token_cost_keys},
    }


def _engine_attempt_order(default_engine: str, preference: list[str]) -> list[str]:
    seen: set[str] = set()
    order: list[str] = []
    for engine_name in [default_engine, *preference]:
        if engine_name in seen:
            continue
        seen.add(engine_name)
        order.append(engine_name)
    return order


def _coerce_text_list(field: str, value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return list(value)


# ---------------------------------------------------------------------------
# JSON payload validation helpers (used by task-mutation POST endpoints)
# ---------------------------------------------------------------------------


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = _optional_string(payload, key)
    if value is None or not value.strip():
        raise ValueError(f"'{key}' is required")
    return value


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    if key not in payload:
        return None
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string")
    return value


def _optional_string_list(payload: dict[str, Any], key: str) -> list[str] | None:
    if key not in payload:
        return None
    return _coerce_text_list(key, payload[key])


def _field_or_missing(payload: dict[str, Any], key: str) -> object:
    if key not in payload:
        return ...
    return payload[key]


def _list_field_or_missing(payload: dict[str, Any], key: str) -> object:
    if key not in payload:
        return ...
    return _coerce_text_list(key, payload[key])


def _list_or_missing(value: object, key: str) -> object:
    if value is ...:
        return ...
    return _coerce_text_list(key, value)


def _optional_bool(payload: dict[str, Any], key: str, *, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"'{key}' must be a boolean")
    return value


def _optional_update_priority(payload: dict[str, Any]) -> object:
    if "priority" not in payload:
        return ...
    value = payload["priority"]
    if value in {None, ""}:
        return ...
    return value
