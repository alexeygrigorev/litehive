"""Local-only HTTP monitor for queue, task, and session artifacts."""


from dataclasses import asdict, dataclass
from functools import partial
import gzip
import hashlib
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml
from jinja2 import Environment, FileSystemLoader

from litehive.config import config_path, load_config
from litehive.models import TaskRecord
from litehive.observability import load_engine_monitoring
from litehive.runtime import active_engine_freezes
from litehive.events import read_events
from litehive.tasks import (
    VALID_TASK_ENGINES,
    VALID_TASK_PRIORITIES,
    VALID_TASK_TYPES,
    list_tasks_state_first,
    load_state,
    load_task_thread,
    require_task,
    runner_status_readonly,
    switch_task_engine,
    task_dir,
    update_task,
)

_POLL_INTERVAL_MS = 1500
_MAX_ARTIFACT_BYTES = 64 * 1024
_RUN_ALL_PREVIEW_BYTES = 32 * 1024
_RUN_ALL_LOG_LIMIT = 8
_SESSION_LIMIT = 12
_STREAM_KEEPALIVE_SECONDS = 15.0
_STREAM_RETRY_MS = 2000
_STREAM_SCAN_INTERVAL_SECONDS = 0.25

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
    runner = runner_status_readonly(root)
    tasks = list_tasks_state_first(root, state=state, include_runtime=True)
    tasks_payload = [_serialize_task(root, task, state.active_task_id) for task in tasks]
    active_task = next((task for task in tasks if task.id == state.active_task_id), None)
    return {
        "workspace": str(root),
        "generated_at": _read_iso_now(),
        "runner": runner.model_dump(mode="python"),
        "state": state.model_dump(mode="python"),
        "editable_fields": {
            "priority_options": sorted(VALID_TASK_PRIORITIES),
            "engine_options": sorted(VALID_TASK_ENGINES),
        },
        "queue": list(state.queue),
        "active_task_id": state.active_task_id,
        "active_task": None if active_task is None else _serialize_task(root, active_task, state.active_task_id),
        "tasks": tasks_payload,
        "run_all_logs": list_recent_run_all_logs(root),
    }


def read_engine_dashboard(root: Path) -> dict[str, Any]:
    root = root.resolve()
    config = load_config(root)
    monitoring = load_engine_monitoring(root)
    active_freezes = active_engine_freezes(config)
    default_fallback_order = _engine_attempt_order(config.default_engine, config.engine_preference)
    return {
        "config": {
            "default_engine": config.default_engine,
            "recovery_engine": config.recovery_engine,
            "claude_enabled": config.claude_enabled,
            "engine_preference": list(config.engine_preference),
            "engine_freeze": dict(config.engine_freeze),
            "active_engine_freezes": {
                engine_name: freeze_dt.replace(microsecond=0).isoformat()
                for engine_name, freeze_dt in sorted(active_freezes.items())
            },
            "models": {
                "codex": config.codex_model,
                "opencode": config.opencode_model,
                "gemini": config.gemini_model,
                "copilot": config.copilot_model,
                "claude": config.claude_model if config.claude_enabled else None,
                "goz": config.goz_model,
            },
            "engine_usage_caps": dict(config.engine_usage_caps),
            "engine_budget_caps": dict(config.engine_budget_caps),
            "engine_costs": dict(config.engine_costs),
        },
        "routing": {
            "precedence": [
                {
                    "order": 1,
                    "rule": "task_override",
                    "description": "Task engine override wins when task.yaml sets engine.",
                },
                {
                    "order": 2,
                    "rule": "workspace_default",
                    "description": "Otherwise Litehive uses the workspace default engine.",
                },
                {
                    "order": 3,
                    "rule": "fallback_preference",
                    "description": "Retries and execution-limit fallbacks follow engine_preference after the selected primary engine.",
                },
                {
                    "order": 4,
                    "rule": "freeze_filter",
                    "description": "Engines with an active freeze are skipped from the runnable attempt order.",
                },
            ],
            "default_fallback_order": default_fallback_order,
            "task_types": [
                {
                    "task_type": task_type,
                    "configured_engine": None,
                    "effective_engine": config.default_engine,
                    "fallback_order": default_fallback_order,
                    "source": "workspace default",
                }
                for task_type in sorted(VALID_TASK_TYPES)
            ],
        },
        "monitoring": {
            "engines": {
                engine_name: _serialize_engine_record(record.model_dump(mode="python"))
                for engine_name, record in sorted(monitoring.engines.items())
            }
        },
        "editable_fields": {
            "engine_options": sorted(VALID_TASK_ENGINES),
        },
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
    server.workspace_stream_monitor = WorkspaceStreamMonitor(root)
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
        if parsed.path == "/api/engines":
            return self._send_json(read_engine_dashboard(self.workspace_root))
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
        if parsed.path == "/api/stream":
            params = parse_qs(parsed.query)
            task_id = params.get("task_id", [None])[0]
            subagent_id = params.get("subagent_id", [None])[0]
            return self._stream_events(task_id=task_id, subagent_id=subagent_id)
        return self._send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        if parsed.path == "/api/task":
            task_id = payload.get("task_id")
            updates = payload.get("updates")
            if not isinstance(task_id, str) or not task_id:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "task_id is required")
            if not isinstance(updates, dict):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "updates must be an object")
            try:
                response = update_task_detail(self.workspace_root, task_id, updates)
            except FileNotFoundError as exc:
                return self._send_error_json(HTTPStatus.NOT_FOUND, str(exc))
            except ValueError as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send_json(response)
        if parsed.path == "/api/engines/default":
            engine_name = payload.get("engine")
            if not isinstance(engine_name, str) or not engine_name:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "engine is required")
            try:
                response = update_default_engine(self.workspace_root, engine_name)
            except ValueError as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send_json(response)
        if parsed.path == "/api/task/engine":
            task_id = payload.get("task_id")
            engine_name = payload.get("engine")
            reason = payload.get("reason")
            if not isinstance(task_id, str) or not task_id:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "task_id is required")
            if not isinstance(engine_name, str) or not engine_name:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "engine is required")
            if not isinstance(reason, str) or not reason.strip():
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "reason is required")
            try:
                response = switch_task_engine_via_web(
                    self.workspace_root,
                    task_id=task_id,
                    engine=engine_name,
                    reason=reason,
                )
            except FileNotFoundError as exc:
                return self._send_error_json(HTTPStatus.NOT_FOUND, str(exc))
            except ValueError as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send_json(response)
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

    def _read_json_body(self) -> dict[str, Any]:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            raise ValueError("missing request body")
        try:
            raw = self.rfile.read(int(content_length))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _stream_events(self, *, task_id: str | None, subagent_id: str | None) -> None:
        monitor = self.server.workspace_stream_monitor
        selection = StreamSelection(task_id=task_id, subagent_id=subagent_id)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(f"retry: {_STREAM_RETRY_MS}\n\n".encode("utf-8"))
            self.wfile.flush()

            state = monitor.build_initial_state(selection)
            self._write_sse("snapshot", {"kind": "initial", **state.snapshot_event}, event_id=str(state.revision))
            if state.session_event is not None:
                self._write_sse("session", {"kind": "initial", **state.session_event}, event_id=str(state.revision))

            last_revision = state.revision
            last_emit = time.monotonic()
            while True:
                revision = monitor.wait_for_change(last_revision, timeout=_STREAM_KEEPALIVE_SECONDS)
                if revision == last_revision:
                    self._write_sse_comment("keepalive")
                    last_emit = time.monotonic()
                    continue

                state = monitor.build_state(selection, revision=revision)
                if state.snapshot_event is not None:
                    self._write_sse("snapshot", {"kind": "update", **state.snapshot_event}, event_id=str(state.revision))
                    last_emit = time.monotonic()
                if state.session_event is not None:
                    self._write_sse("session", {"kind": "update", **state.session_event}, event_id=str(state.revision))
                    last_emit = time.monotonic()
                if time.monotonic() - last_emit >= _STREAM_KEEPALIVE_SECONDS:
                    self._write_sse_comment("keepalive")
                    last_emit = time.monotonic()
                last_revision = state.revision
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def _write_sse(self, event: str, payload: dict[str, Any], *, event_id: str) -> None:
        encoded = json.dumps(payload, separators=(",", ":"))
        message = f"id: {event_id}\nevent: {event}\ndata: {encoded}\n\n".encode("utf-8")
        self.wfile.write(message)
        self.wfile.flush()

    def _write_sse_comment(self, comment: str) -> None:
        self.wfile.write(f": {comment}\n\n".encode("utf-8"))
        self.wfile.flush()


@dataclass(slots=True)
class StreamSelection:
    task_id: str | None
    subagent_id: str | None


@dataclass(slots=True)
class StreamState:
    revision: int
    snapshot_signature: str
    session_signature: str | None
    snapshot_event: dict[str, Any] | None
    session_event: dict[str, Any] | None


class WorkspaceStreamMonitor:
    """Shared fingerprint scanner that lets SSE clients wait for workspace changes."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._condition = threading.Condition()
        self._revision = 0
        self._workspace_signature = self._compute_workspace_signature()
        self._states: dict[tuple[str | None, str | None], StreamState] = {}
        self._thread = threading.Thread(target=self._watch_loop, name="litehive-web-stream", daemon=True)
        self._thread.start()

    def wait_for_change(self, revision: int, *, timeout: float) -> int:
        with self._condition:
            self._condition.wait_for(lambda: self._revision != revision, timeout=timeout)
            return self._revision

    def build_initial_state(self, selection: StreamSelection) -> StreamState:
        return self._build_state(selection, revision=self._revision, force_snapshot=True, force_session=True)

    def build_state(self, selection: StreamSelection, *, revision: int) -> StreamState:
        return self._build_state(selection, revision=revision, force_snapshot=False, force_session=False)

    def _watch_loop(self) -> None:
        while True:
            signature = self._compute_workspace_signature()
            with self._condition:
                if signature != self._workspace_signature:
                    self._workspace_signature = signature
                    self._revision += 1
                    self._condition.notify_all()
            time.sleep(_STREAM_SCAN_INTERVAL_SECONDS)

    def _build_state(
        self,
        selection: StreamSelection,
        *,
        revision: int,
        force_snapshot: bool,
        force_session: bool,
    ) -> StreamState:
        key = (selection.task_id, selection.subagent_id)
        previous = self._states.get(key)
        snapshot = build_workspace_snapshot(self.root)
        snapshot_signature = _stable_signature(snapshot)
        snapshot_event: dict[str, Any] | None = None
        if force_snapshot or previous is None or previous.snapshot_signature != snapshot_signature:
            snapshot_event = {
                "snapshot": snapshot,
                "diff": _workspace_snapshot_diff(
                    None if previous is None else previous.snapshot_event["snapshot"],
                    snapshot,
                ),
            }

        session_payload: dict[str, Any] | None = None
        session_signature: str | None = None
        session_event: dict[str, Any] | None = None
        if selection.task_id and selection.subagent_id:
            try:
                session_payload = read_session_view(self.root, selection.task_id, selection.subagent_id)
            except FileNotFoundError:
                session_payload = None
            session_signature = _stable_signature(session_payload)
            if force_session or previous is None or previous.session_signature != session_signature:
                session_event = {
                    "task_id": selection.task_id,
                    "subagent_id": selection.subagent_id,
                    "session": session_payload,
                    "diff": _session_payload_diff(
                        None if previous is None else None if previous.session_event is None else previous.session_event["session"],
                        session_payload,
                    ),
                }

        state = StreamState(
            revision=revision,
            snapshot_signature=snapshot_signature,
            session_signature=session_signature,
            snapshot_event=snapshot_event if snapshot_event is not None else None if previous is None else previous.snapshot_event,
            session_event=session_event if session_event is not None else None if previous is None else previous.session_event,
        )
        self._states[key] = state
        return StreamState(
            revision=revision,
            snapshot_signature=snapshot_signature,
            session_signature=session_signature,
            snapshot_event=snapshot_event,
            session_event=session_event,
        )

    def _compute_workspace_signature(self) -> str:
        digest = hashlib.sha256()
        for path in _iter_stream_paths(self.root):
            digest.update(str(_relative_to_root(self.root, path)).encode("utf-8"))
            try:
                stat = path.stat()
            except FileNotFoundError:
                digest.update(b"missing")
                continue
            digest.update(str(stat.st_mtime_ns).encode("utf-8"))
            digest.update(str(stat.st_size).encode("utf-8"))
        return digest.hexdigest()


def update_task_detail(root: Path, task_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    task = _require_task_for_web(root, task_id)
    payload: dict[str, Any] = {}
    for field in ("goal", "priority", "engine"):
        if field in updates:
            value = updates[field]
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field} must be a string or null")
            payload[field] = value
    for field in ("acceptance_criteria", "constraints", "plan"):
        if field in updates:
            payload[field] = _coerce_text_list(field, updates[field])
    if not payload:
        raise ValueError("No supported fields to update")
    updated = update_task(
        root,
        task.id,
        journal_message="Task metadata updated via web dashboard.",
        **payload,
    )
    return {"task": _serialize_task(root, updated, load_state(root).active_task_id)}


def update_default_engine(root: Path, engine_name: str) -> dict[str, Any]:
    root = root.resolve()
    if engine_name not in VALID_TASK_ENGINES:
        raise ValueError(f"Unsupported engine '{engine_name}'")
    path = config_path(root)
    raw_data = _load_yaml_file(path)
    config = load_config(root)
    previous_engine = config.default_engine
    raw_data["default_engine"] = engine_name
    path.write_text(yaml.safe_dump(raw_data, sort_keys=False), encoding="utf-8")
    return {
        "default_engine": engine_name,
        "previous_default_engine": previous_engine,
        "engines": read_engine_dashboard(root),
    }


def switch_task_engine_via_web(
    root: Path,
    *,
    task_id: str,
    engine: str,
    reason: str,
) -> dict[str, Any]:
    root = root.resolve()
    try:
        summary = switch_task_engine(root, task_id, engine=engine, reason=reason)
    except ValueError as exc:
        if str(exc).startswith("Task ") and str(exc).endswith("not found"):
            raise FileNotFoundError(str(exc)) from exc
        raise
    task = require_task(root, task_id)
    return {
        "task": _serialize_task(root, task, load_state(root).active_task_id),
        "switch": {
            "previous_engine": summary.previous_engine,
            "new_engine": summary.new_engine,
            "was_active": summary.was_active,
            "runner_pid": summary.runner_pid,
            "signal_sent": summary.signal_sent,
            "prior_work_paths": list(summary.prior_work_paths),
        },
    }


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

    reports = _load_stage_reports(root, base)
    recovery_reports = _load_recovery_reports(root, base)
    thread = [
        comment.model_dump(mode="python")
        for comment in load_task_thread(root, task)
    ]
    events = _load_task_events(root, task)
    record = task.model_dump(mode="python", exclude={"runtime"})

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
        "thread_file": _relative_to_root(root, base / "thread.yaml"),
        "events_file": _relative_to_root(root, base / "events.jsonl"),
        "reports_dir": _relative_to_root(root, base / "reports"),
        "recovery_dir": _relative_to_root(root, base / "recovery"),
        "record": record,
        "reports": reports,
        "events": events,
        "thread": thread,
        "recovery_reports": recovery_reports,
        "subagents": session_entries,
    }


def _require_task_for_web(root: Path, task_id: str) -> TaskRecord:
    try:
        return require_task(root, task_id)
    except ValueError as exc:
        raise FileNotFoundError(str(exc)) from exc


def _coerce_text_list(field: str, value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return list(value)


def _load_stage_reports(root: Path, base: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    reports_dir = base / "reports"
    if not reports_dir.exists():
        return reports
    for report_path in sorted(reports_dir.glob("*.yaml")):
        payload = _load_yaml_file(report_path)
        reports.append(
            {
                "name": report_path.name,
                "path": _relative_to_root(root, report_path),
                "step": payload.get("step"),
                "verdict": payload.get("verdict"),
                "summary": payload.get("summary"),
                "created_at": payload.get("created_at"),
                "report": payload,
            }
        )
    return reports


def _load_recovery_reports(root: Path, base: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    recovery_dir = base / "recovery"
    if not recovery_dir.exists():
        return reports
    for report_path in sorted(recovery_dir.glob("*.yaml")):
        payload = _load_yaml_file(report_path)
        reports.append(
            {
                "name": report_path.name,
                "path": _relative_to_root(root, report_path),
                "summary": payload.get("summary"),
                "trigger": payload.get("trigger"),
                "stage": payload.get("stage"),
                "runnable_state": payload.get("runnable_state"),
                "created_at": payload.get("created_at"),
                "report": payload,
            }
        )
    return reports


def _load_task_events(root: Path, task: TaskRecord) -> list[dict[str, Any]]:
    events = read_events(root, task)
    events.sort(key=lambda item: (
        str(item.get("ts") or ""),
        int(item.get("sequence", 0)) if isinstance(item.get("sequence", 0), int) else 0,
    ))
    return events


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


def _engine_attempt_order(default_engine: str, preference: list[str]) -> list[str]:
    seen: set[str] = set()
    order: list[str] = []
    for engine_name in [default_engine, *preference]:
        if engine_name in seen:
            continue
        seen.add(engine_name)
        order.append(engine_name)
    return order


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


def _stable_signature(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _workspace_snapshot_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
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
        "runner_changed": None if previous is None else previous.get("runner") != current.get("runner"),
        "queue_changed": None if previous is None else previous.get("queue") != current.get("queue"),
        "added_task_ids": sorted(current_task_ids - previous_task_ids),
        "removed_task_ids": sorted(previous_task_ids - current_task_ids),
        "changed_task_ids": changed_tasks,
    }


def _session_payload_diff(previous: dict[str, Any] | None, current: dict[str, Any] | None) -> dict[str, Any]:
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
