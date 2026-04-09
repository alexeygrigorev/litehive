from dataclasses import dataclass
from functools import partial
import hashlib
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from litehive.tasks import WorkspaceConflictError
from litehive.web.common import (
    _STREAM_KEEPALIVE_SECONDS,
    _STREAM_RETRY_MS,
    _STREAM_SCAN_INTERVAL_SECONDS,
    _render_index,
    _iter_stream_paths,
    _relative_to_root,
    _session_payload_diff,
    _stable_signature,
    _workspace_snapshot_diff,
)
import litehive.web as _web_pkg


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
            return self._send_json(_web_pkg.build_workspace_snapshot(self.workspace_root))
        if parsed.path == "/api/engines":
            return self._send_json(_web_pkg.read_engine_dashboard(self.workspace_root))
        if parsed.path == "/api/session":
            params = parse_qs(parsed.query)
            task_id = params.get("task_id", [None])[0]
            subagent_id = params.get("subagent_id", [None])[0]
            if not task_id or not subagent_id:
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST, "task_id and subagent_id are required"
                )
            try:
                payload = _web_pkg.read_session_view(self.workspace_root, task_id, subagent_id)
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
                response = _web_pkg.update_task_detail(self.workspace_root, task_id, updates)
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
                response = _web_pkg.update_default_engine(self.workspace_root, engine_name)
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
                response = _web_pkg.switch_task_engine_via_web(
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
        if parsed.path == "/api/report":
            role = payload.get("role")
            step = payload.get("step")
            verdict = payload.get("verdict")
            message = payload.get("message")
            task_id = payload.get("task_id")
            if task_id is not None and not isinstance(task_id, str):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "task_id must be a string")
            if not isinstance(role, str) or not role.strip():
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "role is required")
            if not isinstance(step, str) or not step.strip():
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "step is required")
            if not isinstance(verdict, str) or not verdict.strip():
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "verdict is required")
            if not isinstance(message, str) or not message.strip():
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "message is required")
            try:
                response = _web_pkg.submit_stage_verdict_via_web(
                    self.workspace_root,
                    task_id=task_id,
                    role=role,
                    step=step,
                    verdict=verdict,
                    message=message,
                )
            except FileNotFoundError as exc:
                return self._send_error_json(HTTPStatus.NOT_FOUND, str(exc))
            except ValueError as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send_json(response)
        # --- Task mutation endpoints ---
        if parsed.path == "/api/tasks":
            return self._handle_task_mutation(
                lambda: _web_pkg.create_task_via_web(self.workspace_root, payload),
                status=HTTPStatus.CREATED,
            )
        if parsed.path == "/api/tasks/active/stop":
            return self._handle_task_mutation(
                lambda: _web_pkg.stop_active_task_via_web(self.workspace_root, payload),
            )
        task_id, action = self._parse_task_action_path(parsed.path)
        if task_id is not None and action is not None:
            handlers = {
                "update": lambda: _web_pkg.update_task_via_web(
                    self.workspace_root, task_id, payload
                ),
                "close": lambda: _web_pkg.close_task_via_web(
                    self.workspace_root, task_id, payload
                ),
                "requeue": lambda: _web_pkg.requeue_task_via_web(
                    self.workspace_root, task_id, payload
                ),
                "abandon": lambda: _web_pkg.abandon_task_via_web(
                    self.workspace_root, task_id, payload
                ),
            }
            handler = handlers.get(action)
            if handler is not None:
                return self._handle_task_mutation(handler)
        return self._send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    @staticmethod
    def _parse_task_action_path(path: str) -> tuple[str | None, str | None]:
        parts = [part for part in path.split("/") if part]
        if len(parts) != 4 or parts[:2] != ["api", "tasks"]:
            return None, None
        return parts[2], parts[3]

    def _handle_task_mutation(
        self,
        handler: Any,
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        try:
            response = handler()
        except FileNotFoundError as exc:
            return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except WorkspaceConflictError as exc:
            return self._send_error_json(HTTPStatus.CONFLICT, str(exc))
        except ValueError as exc:
            return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        return self._send_json(response, status=status)

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        payload = json.dumps({"ok": False, "error": message}).encode("utf-8")
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
            self._write_sse(
                "snapshot",
                {"kind": "initial", **state.snapshot_event},
                event_id=str(state.revision),
            )
            if state.session_event is not None:
                self._write_sse(
                    "session",
                    {"kind": "initial", **state.session_event},
                    event_id=str(state.revision),
                )

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
                    self._write_sse(
                        "snapshot",
                        {"kind": "update", **state.snapshot_event},
                        event_id=str(state.revision),
                    )
                    last_emit = time.monotonic()
                if state.session_event is not None:
                    self._write_sse(
                        "session",
                        {"kind": "update", **state.session_event},
                        event_id=str(state.revision),
                    )
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
        self._thread = threading.Thread(
            target=self._watch_loop, name="litehive-web-stream", daemon=True
        )
        self._thread.start()

    def wait_for_change(self, revision: int, *, timeout: float) -> int:
        with self._condition:
            self._condition.wait_for(lambda: self._revision != revision, timeout=timeout)
            return self._revision

    def build_initial_state(self, selection: StreamSelection) -> StreamState:
        return self._build_state(
            selection, revision=self._revision, force_snapshot=True, force_session=True
        )

    def build_state(self, selection: StreamSelection, *, revision: int) -> StreamState:
        return self._build_state(
            selection, revision=revision, force_snapshot=False, force_session=False
        )

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
        snapshot = _web_pkg.build_workspace_snapshot(self.root)
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
                session_payload = _web_pkg.read_session_view(
                    self.root, selection.task_id, selection.subagent_id
                )
            except FileNotFoundError:
                session_payload = None
            session_signature = _stable_signature(session_payload)
            if force_session or previous is None or previous.session_signature != session_signature:
                session_event = {
                    "task_id": selection.task_id,
                    "subagent_id": selection.subagent_id,
                    "session": session_payload,
                    "diff": _session_payload_diff(
                        None
                        if previous is None
                        else None
                        if previous.session_event is None
                        else previous.session_event["session"],
                        session_payload,
                    ),
                }

        state = StreamState(
            revision=revision,
            snapshot_signature=snapshot_signature,
            session_signature=session_signature,
            snapshot_event=snapshot_event
            if snapshot_event is not None
            else None
            if previous is None
            else previous.snapshot_event,
            session_event=session_event
            if session_event is not None
            else None
            if previous is None
            else previous.session_event,
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
