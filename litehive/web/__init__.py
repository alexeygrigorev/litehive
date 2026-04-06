"""Local-only HTTP monitor for queue, task, and session artifacts."""


from dataclasses import asdict, dataclass
from functools import partial
import gzip
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml
from jinja2 import Environment, FileSystemLoader

from litehive.models import TaskRecord
from litehive.tasks import list_tasks_state_first, load_state, runner_status, task_dir

_POLL_INTERVAL_MS = 1500
_MAX_ARTIFACT_BYTES = 64 * 1024
_RUN_ALL_PREVIEW_BYTES = 32 * 1024
_RUN_ALL_LOG_LIMIT = 8
_SESSION_LIMIT = 12

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


def build_workspace_snapshot(root: Path) -> dict[str, Any]:
    """Return a JSON-serializable monitor snapshot backed by on-disk workspace state."""
    root = root.resolve()
    state = load_state(root)
    runner = runner_status(root)
    tasks = list_tasks_state_first(root, state=state, include_runtime=True)
    tasks_payload = [_serialize_task(root, task, state.active_task_id) for task in tasks]
    active_task = next((task for task in tasks if task.id == state.active_task_id), None)
    return {
        "workspace": str(root),
        "generated_at": _read_iso_now(),
        "runner": runner.model_dump(mode="python"),
        "state": state.model_dump(mode="python"),
        "queue": list(state.queue),
        "active_task_id": state.active_task_id,
        "active_task": None if active_task is None else _serialize_task(root, active_task, state.active_task_id),
        "tasks": tasks_payload,
        "run_all_logs": list_recent_run_all_logs(root),
    }


def list_recent_run_all_logs(root: Path, *, limit: int = _RUN_ALL_LOG_LIMIT) -> list[dict[str, Any]]:
    logs_root = root / ".litehive" / "logs" / "run-all"
    if not logs_root.exists():
        return []
    entries: list[dict[str, Any]] = []
    for directory in sorted((path for path in logs_root.iterdir() if path.is_dir()), reverse=True)[:limit]:
        files: list[dict[str, Any]] = []
        for file_path in sorted((path for path in directory.iterdir() if path.is_file())):
            preview = _read_text_file(file_path, max_bytes=_RUN_ALL_PREVIEW_BYTES)
            files.append(
                {
                    "name": file_path.name,
                    "path": _relative_to_root(root, file_path),
                    "size": file_path.stat().st_size,
                    "modified_at": _mtime(file_path),
                    "preview": preview["content"],
                    "truncated": preview["truncated"],
                }
            )
        entries.append(
            {
                "name": directory.name,
                "path": _relative_to_root(root, directory),
                "modified_at": _mtime(directory),
                "files": files,
            }
        )
    return entries


def read_session_view(root: Path, task_id: str, subagent_id: str) -> dict[str, Any]:
    root = root.resolve()
    task = next((item for item in list_tasks_state_first(root, include_runtime=True) if item.id == task_id), None)
    if task is None:
        raise FileNotFoundError(f"Task {task_id} not found")
    ref = next((item for item in task.subagents if item.id == subagent_id), None)
    if ref is None:
        raise FileNotFoundError(f"Subagent {subagent_id} not found for task {task_id}")

    base = task_dir(root, task) / ref.path
    session = _load_yaml_file(base / "session.yaml")
    active_subagent = task.runtime.active_subagent
    is_active = bool(active_subagent and active_subagent.id == subagent_id)
    status = str(session.get("status") or ("running" if is_active else ref.status))
    artifacts = [
        _read_session_artifact(root, base, "transcript", active=is_active),
        _read_session_artifact(root, base, "stdout", active=is_active),
        _read_session_artifact(root, base, "stderr", active=is_active),
    ]
    return {
        "task_id": task.id,
        "task_title": task.title,
        "subagent_id": ref.id,
        "role": ref.role,
        "engine": ref.engine,
        "status": status,
        "is_active": is_active,
        "session_path": _relative_to_root(root, base / "session.yaml"),
        "session": session,
        "artifacts": [asdict(artifact) for artifact in artifacts],
    }


def serve_monitor(root: Path, *, host: str = "127.0.0.1", port: int = 8765) -> int:
    root = root.resolve()
    server = ThreadingHTTPServer((host, port), partial(LitehiveWebHandler, workspace_root=root))
    bound_host, bound_port = server.server_address[:2]
    print(f"Litehive web monitor serving {root}")
    print(f"URL: http://{bound_host}:{bound_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


class LitehiveWebHandler(BaseHTTPRequestHandler):
    """Simple HTML + JSON API monitor for the local workspace."""

    server_version = "LitehiveWeb/0.1"

    def __init__(self, *args: Any, workspace_root: Path, **kwargs: Any) -> None:
        self.workspace_root = workspace_root
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._send_html(_render_index())
        if parsed.path == "/api/snapshot":
            return self._send_json(build_workspace_snapshot(self.workspace_root))
        if parsed.path == "/api/session":
            params = parse_qs(parsed.query)
            task_id = params.get("task_id", [None])[0]
            subagent_id = params.get("subagent_id", [None])[0]
            if not task_id or not subagent_id:
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST, "task_id and subagent_id are required"
                )
            try:
                payload = read_session_view(self.workspace_root, task_id, subagent_id)
            except FileNotFoundError as exc:
                return self._send_error_json(HTTPStatus.NOT_FOUND, str(exc))
            return self._send_json(payload)
        return self._send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        payload = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _serialize_task(root: Path, task: TaskRecord, active_task_id: str | None) -> dict[str, Any]:
    base = task_dir(root, task)
    session_entries: list[dict[str, Any]] = []
    sorted_refs = sorted(task.subagents, key=lambda item: item.id, reverse=True)[:_SESSION_LIMIT]
    active_subagent = task.runtime.active_subagent
    last_subagent = task.runtime.last_subagent
    for ref in sorted_refs:
        session_base = base / ref.path
        session = _load_yaml_file(session_base / "session.yaml")
        is_active = bool(active_subagent and active_subagent.id == ref.id)
        artifact_sources = {
            "transcript": _artifact_path(root, session_base, "transcript", active=is_active),
            "stdout": _artifact_path(root, session_base, "stdout", active=is_active),
            "stderr": _artifact_path(root, session_base, "stderr", active=is_active),
        }
        session_entries.append(
            {
                "id": ref.id,
                "role": ref.role,
                "engine": ref.engine,
                "status": str(session.get("status") or ref.status),
                "is_active": is_active,
                "path": ref.path,
                "updated_at": session.get("updated_at"),
                "pid": session.get("pid"),
                "exit_code": session.get("exit_code"),
                "tail_targets": artifact_sources,
            }
        )

    reports = []
    reports_dir = base / "reports"
    if reports_dir.exists():
        for report_path in sorted(reports_dir.glob("*.yaml"), reverse=True)[:5]:
            payload = _load_yaml_file(report_path)
            reports.append(
                {
                    "name": report_path.name,
                    "path": _relative_to_root(root, report_path),
                    "step": payload.get("step"),
                    "verdict": payload.get("verdict"),
                    "summary": payload.get("summary"),
                }
            )

    return {
        "id": task.id,
        "slug": task.slug,
        "title": task.title,
        "status": task.status,
        "pipeline_status": task.pipeline_status,
        "priority": task.priority,
        "goal": task.goal,
        "acceptance_criteria": list(task.acceptance_criteria),
        "plan": list(task.plan),
        "constraints": list(task.constraints),
        "is_active_task": task.id == active_task_id,
        "runtime": task.runtime.model_dump(mode="python"),
        "current_stage": task.runtime.current_stage.model_dump(mode="python"),
        "last_stage": task.runtime.last_stage.model_dump(mode="python"),
        "active_subagent": None if active_subagent is None else active_subagent.model_dump(mode="python"),
        "last_subagent": None if last_subagent is None else last_subagent.model_dump(mode="python"),
        "task_path": _relative_to_root(root, base),
        "task_file": _relative_to_root(root, base / "task.yaml"),
        "runtime_file": _relative_to_root(root, base / "runtime.yaml"),
        "reports": reports,
        "subagents": session_entries,
    }


def _read_session_artifact(root: Path, base: Path, kind: str, *, active: bool) -> SessionArtifact:
    if kind == "transcript":
        path = _resolve_artifact_path(base, "transcript.md")
        display_path = path if path is not None else base / "transcript.md"
        payload = _read_text_file(path, max_bytes=_MAX_ARTIFACT_BYTES)
        return SessionArtifact(
            kind=kind,
            label="Transcript",
            path=_relative_to_root(root, display_path),
            source="rewrite",
            content=payload["content"],
            size=payload["size"],
            truncated=payload["truncated"],
            available=payload["available"],
        )

    preferred = base / f"{kind}.log" if active else base / f"{kind}.txt"
    fallback = base / f"{kind}.txt"
    gzip_fallback = base / f"{kind}.txt.gz"
    source_path = preferred
    source_type = "append-only log" if active else "final snapshot"
    if not source_path.exists():
        if fallback.exists():
            source_path = fallback
            source_type = "final snapshot"
        elif gzip_fallback.exists():
            source_path = gzip_fallback
            source_type = "compressed final snapshot"
        else:
            source_path = preferred
    payload = _read_text_file(source_path, max_bytes=_MAX_ARTIFACT_BYTES)
    return SessionArtifact(
        kind=kind,
        label=kind.upper(),
        path=_relative_to_root(root, source_path),
        source=source_type,
        content=payload["content"],
        size=payload["size"],
        truncated=payload["truncated"],
        available=payload["available"],
    )


def _artifact_path(root: Path, base: Path, kind: str, *, active: bool) -> str:
    if kind == "transcript":
        path = _resolve_artifact_path(base, "transcript.md")
        return _relative_to_root(root, path if path is not None else base / "transcript.md")
    candidates = [base / f"{kind}.log", base / f"{kind}.txt", base / f"{kind}.txt.gz"] if active else [base / f"{kind}.txt", base / f"{kind}.txt.gz", base / f"{kind}.log"]
    for candidate in candidates:
        if candidate.exists():
            return _relative_to_root(root, candidate)
    return _relative_to_root(root, candidates[0])


def _resolve_artifact_path(base: Path, name: str) -> Path | None:
    direct = base / name
    if direct.exists():
        return direct
    gzip_path = base / f"{name}.gz"
    if gzip_path.exists():
        return gzip_path
    return None


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
