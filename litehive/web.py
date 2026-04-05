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

from litehive.models import TaskRecord
from litehive.tasks import list_tasks_state_first, load_state, runner_status, task_dir

_POLL_INTERVAL_MS = 1500
_MAX_ARTIFACT_BYTES = 64 * 1024
_RUN_ALL_PREVIEW_BYTES = 32 * 1024
_RUN_ALL_LOG_LIMIT = 8
_SESSION_LIMIT = 12


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
            return self._send_html(_INDEX_HTML)
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


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Litehive Monitor</title>
  <style>
    :root {{
      --bg: #f3efe6;
      --panel: #fffaf2;
      --ink: #1e2220;
      --muted: #6c7067;
      --line: #d9d1c2;
      --accent: #165d52;
      --accent-soft: #d8eee7;
      --warn: #925100;
      --warn-soft: #fff0cf;
      --danger: #8d2d1f;
      --danger-soft: #f8ddd7;
      --mono: "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
      --sans: "Iosevka Aile", "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: var(--sans);
      color: var(--ink);
      background:
        radial-gradient(circle at top left, #fff8e8 0, transparent 30%),
        linear-gradient(180deg, #efe7d9 0, var(--bg) 28%, #ede7dd 100%);
    }}
    header {{
      padding: 20px 24px 12px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 250, 242, 0.9);
      backdrop-filter: blur(12px);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1, h2, h3 {{ margin: 0; }}
    h1 {{
      font-size: 28px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    main {{
      display: grid;
      grid-template-columns: 340px minmax(420px, 1fr) minmax(360px, 1fr);
      gap: 16px;
      padding: 16px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 10px 30px rgba(66, 43, 14, 0.07);
      overflow: hidden;
      min-height: 180px;
    }}
    .panel > .hd {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(255,255,255,0.8), rgba(247,241,231,0.9));
    }}
    .panel > .bd {{ padding: 14px 16px; }}
    .task-list {{
      display: grid;
      gap: 10px;
      max-height: calc(100vh - 170px);
      overflow: auto;
      padding: 14px;
    }}
    button.task {{
      width: 100%;
      text-align: left;
      border: 1px solid var(--line);
      background: white;
      border-radius: 14px;
      padding: 12px;
      cursor: pointer;
      transition: transform 140ms ease, border-color 140ms ease, box-shadow 140ms ease;
    }}
    button.task:hover {{
      transform: translateY(-1px);
      border-color: var(--accent);
      box-shadow: 0 8px 18px rgba(22, 93, 82, 0.09);
    }}
    button.task.active {{
      border-color: var(--accent);
      background: var(--accent-soft);
    }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      border-radius: 999px;
      font-size: 12px;
      border: 1px solid var(--line);
      background: #fff;
    }}
    .badge.running {{ background: var(--accent-soft); border-color: #9acaba; color: var(--accent); }}
    .badge.completed {{ background: #eef4ec; border-color: #b9cfb4; color: #2f6842; }}
    .badge.failed, .badge.blocked, .badge.interrupted {{ background: var(--danger-soft); border-color: #e0a193; color: var(--danger); }}
    .badge.parked {{ background: var(--warn-soft); border-color: #e1c06e; color: var(--warn); }}
    .badge.late, .badge.stale {{ background: var(--warn-soft); border-color: #e1c06e; color: var(--warn); }}
    .kv {{ display: grid; grid-template-columns: 140px 1fr; gap: 8px 12px; font-size: 14px; }}
    .kv div:nth-child(odd) {{ color: var(--muted); }}
    .section {{ margin-top: 18px; }}
    .section:first-child {{ margin-top: 0; }}
    ul.flat {{ list-style: none; padding: 0; margin: 8px 0 0; display: grid; gap: 8px; }}
    li.card {{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 10px 12px;
      background: white;
    }}
    .artifact {{
      margin-top: 14px;
      border: 1px solid var(--line);
      border-radius: 14px;
      overflow: hidden;
      background: #fdfcf8;
    }}
    .artifact .bar {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      font-size: 12px;
      color: var(--muted);
    }}
    pre {{
      margin: 0;
      padding: 12px;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 240px;
      overflow: auto;
      background: #171b19;
      color: #eff7f3;
    }}
    .logs {{ display: grid; gap: 12px; }}
    .muted {{ color: var(--muted); }}
    .mono {{ font-family: var(--mono); }}
    @media (max-width: 1200px) {{
      main {{ grid-template-columns: 1fr; }}
      .task-list {{ max-height: none; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Litehive Monitor</h1>
    <div class="meta">
      <span id="runner-badge-slot"><span class="badge" id="runner-badge">loading</span></span>
      <span class="badge mono" id="workspace-path">...</span>
      <span class="badge mono">poll __POLL_SECONDS__s</span>
    </div>
  </header>
  <main>
    <section class="panel">
      <div class="hd">
        <h2>Queue</h2>
        <span class="muted mono" id="queue-summary"></span>
      </div>
      <div class="task-list" id="task-list"></div>
    </section>
    <section class="panel">
      <div class="hd">
        <h2>Task</h2>
        <span class="muted mono" id="task-summary"></span>
      </div>
      <div class="bd" id="task-detail"></div>
    </section>
    <section class="panel">
      <div class="hd">
        <h2>Live Session + Run-All</h2>
        <span class="muted mono" id="session-summary"></span>
      </div>
      <div class="bd" id="session-detail"></div>
    </section>
  </main>
  <script>
    const state = {{
      snapshot: null,
      selectedTaskId: null,
      selectedSubagentId: null,
    }};

    function esc(value) {{
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }}

    function badge(label, extra = "") {{
      return `<span class="badge ${{extra}}">${{esc(label)}}</span>`;
    }}

    async function fetchJson(url) {{
      const response = await fetch(url, {{ cache: "no-store" }});
      if (!response.ok) {{
        throw new Error(`${{response.status}} ${{response.statusText}}`);
      }}
      return response.json();
    }}

    function chooseDefaults(snapshot) {{
      if (!state.selectedTaskId) {{
        state.selectedTaskId = snapshot.active_task_id || snapshot.queue[0] || snapshot.tasks[0]?.id || null;
      }}
      const task = snapshot.tasks.find((item) => item.id === state.selectedTaskId) || snapshot.tasks[0] || null;
      if (task && (!state.selectedSubagentId || !task.subagents.some((item) => item.id === state.selectedSubagentId))) {{
        state.selectedSubagentId = task.subagents.find((item) => item.is_active)?.id || task.subagents[0]?.id || null;
      }}
      if (!task) {{
        state.selectedTaskId = null;
        state.selectedSubagentId = null;
      }}
    }}

    function renderQueue(snapshot) {{
      const list = document.getElementById("task-list");
      document.getElementById("queue-summary").textContent = `${{snapshot.state.queue.length}} queued`;
      document.getElementById("workspace-path").textContent = snapshot.workspace;
      document.getElementById("runner-badge-slot").innerHTML = badge(
        `runner:${{snapshot.runner.status}}`,
        snapshot.runner.status
      );

      if (!snapshot.tasks.length) {{
        list.innerHTML = `<div class="muted">No tasks in this workspace.</div>`;
        return;
      }}
      list.innerHTML = snapshot.tasks.map((task) => `
        <button class="task ${{task.id === state.selectedTaskId ? "active" : ""}}" data-task-id="${{esc(task.id)}}">
          <div><strong>${{esc(task.id)}}</strong> ${{esc(task.title)}}</div>
          <div class="meta">
            ${{badge(task.status, task.status)}}
            ${{badge(task.pipeline_status)}}
            ${{task.is_active_task ? badge("active task", "running") : ""}}
            ${{task.active_subagent ? badge(`${{task.active_subagent.role}}:${{task.active_subagent.status}}`, task.active_subagent.status) : ""}}
          </div>
        </button>
      `).join("");
      list.querySelectorAll("button.task").forEach((button) => {{
        button.addEventListener("click", () => {{
          state.selectedTaskId = button.dataset.taskId;
          const selected = snapshot.tasks.find((item) => item.id === state.selectedTaskId);
          state.selectedSubagentId = selected?.subagents.find((item) => item.is_active)?.id || selected?.subagents[0]?.id || null;
          render(snapshot);
          refreshSession();
        }});
      }});
    }}

    function renderTask(snapshot) {{
      const task = snapshot.tasks.find((item) => item.id === state.selectedTaskId);
      const container = document.getElementById("task-detail");
      document.getElementById("task-summary").textContent = task ? `${{task.status}} / ${{task.pipeline_status}}` : "no task";
      if (!task) {{
        container.innerHTML = `<div class="muted">Select a task.</div>`;
        return;
      }}

      const reports = task.reports.length ? task.reports.map((report) => `
        <li class="card">
          <strong>${{esc(report.name)}}</strong><br>
          <span class="muted">${{esc(report.step || "-")}} / ${{esc(report.verdict || "-")}}</span><br>
          <span>${{esc(report.summary || "")}}</span><br>
          <span class="mono muted">${{esc(report.path)}}</span>
        </li>
      `).join("") : `<li class="card muted">No reports yet.</li>`;

      const sessions = task.subagents.length ? task.subagents.map((session) => `
        <li class="card">
          <div>
            <button class="task ${{session.id === state.selectedSubagentId ? "active" : ""}}" data-subagent-id="${{esc(session.id)}}" style="padding:0;border:0;background:transparent">
              <strong>${{esc(session.id)}}</strong> ${{esc(session.role)}} / ${{esc(session.engine)}}
            </button>
          </div>
          <div class="meta">
            ${{badge(session.status, session.status)}}
            ${{session.is_active ? badge("actively tailed", "running") : badge("completed snapshot")}}
          </div>
          <div class="muted mono">transcript: ${{esc(session.tail_targets.transcript)}}</div>
          <div class="muted mono">stdout: ${{esc(session.tail_targets.stdout)}}</div>
          <div class="muted mono">stderr: ${{esc(session.tail_targets.stderr)}}</div>
        </li>
      `).join("") : `<li class="card muted">No subagent sessions yet.</li>`;

      container.innerHTML = `
        <div class="section">
          <h3>${{esc(task.id)}} ${{esc(task.title)}}</h3>
          <div class="meta">
            ${{badge(task.status, task.status)}}
            ${{badge(task.pipeline_status)}}
            ${{task.is_active_task ? badge("workspace active", "running") : ""}}
          </div>
        </div>
        <div class="section kv">
          <div>Task dir</div><div class="mono">${{esc(task.task_path)}}</div>
          <div>Task file</div><div class="mono">${{esc(task.task_file)}}</div>
          <div>Runtime file</div><div class="mono">${{esc(task.runtime_file)}}</div>
          <div>Goal</div><div>${{esc(task.goal || "-")}}</div>
          <div>Current stage</div><div>${{esc(task.current_stage.step || "-")}} / ${{esc(task.current_stage.status || "-")}}</div>
          <div>Last stage</div><div>${{esc(task.last_stage.step || "-")}} / ${{esc(task.last_stage.verdict || "-")}}</div>
        </div>
        <div class="section">
          <h3>Acceptance Criteria</h3>
          <ul class="flat">${{(task.acceptance_criteria.length ? task.acceptance_criteria : ["No criteria recorded."]).map((item) => `<li class="card">${{esc(item)}}</li>`).join("")}}</ul>
        </div>
        <div class="section">
          <h3>Subagent Sessions</h3>
          <ul class="flat" id="task-sessions">${{sessions}}</ul>
        </div>
        <div class="section">
          <h3>Recent Reports</h3>
          <ul class="flat">${{reports}}</ul>
        </div>
      `;
      container.querySelectorAll("[data-subagent-id]").forEach((button) => {{
        button.addEventListener("click", () => {{
          state.selectedSubagentId = button.dataset.subagentId;
          render(snapshot);
          refreshSession();
        }});
      }});
    }}

    function renderRunAll(snapshot) {{
      const entries = snapshot.run_all_logs.length ? snapshot.run_all_logs.map((entry) => `
        <div class="artifact">
          <div class="bar">
            <strong>${{esc(entry.name)}}</strong>
            <span class="mono">${{esc(entry.path)}}</span>
          </div>
          <div style="padding:12px">
            ${{entry.files.length ? entry.files.map((file) => `
              <div class="artifact" style="margin-top:10px">
                <div class="bar">
                  <span>${{esc(file.name)}}</span>
                  <span class="mono">${{esc(file.path)}}</span>
                </div>
                <pre>${{esc(file.preview || "(empty)")}}</pre>
              </div>
            `).join("") : `<div class="muted">No files in this run-all directory.</div>`}}
          </div>
        </div>
      `).join("") : `<div class="muted">No run-all logs found under .litehive/logs/run-all/.</div>`;
      return `<div class="section"><h3>Recent Run-All Logs</h3><div class="logs">${{entries}}</div></div>`;
    }}

    function renderSessionPayload(payload, snapshot) {{
      const container = document.getElementById("session-detail");
      document.getElementById("session-summary").textContent = payload
        ? `${{payload.subagent_id}} / ${{payload.status}}`
        : "no session";
      if (!payload) {{
        container.innerHTML = renderRunAll(snapshot);
        return;
      }}
      const artifacts = payload.artifacts.map((artifact) => `
        <div class="artifact">
          <div class="bar">
            <strong>${{esc(artifact.label)}}</strong>
            <span>${{artifact.available ? esc(payload.is_active ? "actively tailed" : "completed snapshot") : "unavailable"}}</span>
          </div>
          <div class="bar">
            <span class="mono">${{esc(artifact.path)}}</span>
            <span>${{esc(artifact.source)}}${{artifact.truncated ? " / tail view" : ""}}</span>
          </div>
          <pre>${{esc(artifact.content || "(empty)")}}</pre>
        </div>
      `).join("");
      container.innerHTML = `
        <div class="section">
          <h3>${{esc(payload.subagent_id)}} ${{esc(payload.role)}} / ${{esc(payload.engine)}}</h3>
          <div class="meta">
            ${{badge(payload.status, payload.status)}}
            ${{payload.is_active ? badge("active session", "running") : badge("completed session")}}
          </div>
          <div class="kv" style="margin-top:12px">
            <div>Session file</div><div class="mono">${{esc(payload.session_path)}}</div>
            <div>PID</div><div>${{esc(payload.session.pid || "-")}}</div>
            <div>Exit code</div><div>${{esc(payload.session.exit_code ?? "-")}}</div>
            <div>Updated</div><div>${{esc(payload.session.updated_at || "-")}}</div>
          </div>
        </div>
        <div class="section">
          <h3>Artifacts</h3>
          ${{artifacts}}
        </div>
        ${{renderRunAll(snapshot)}}
      `;
    }}

    async function refreshSnapshot() {{
      state.snapshot = await fetchJson("/api/snapshot");
      chooseDefaults(state.snapshot);
      render(state.snapshot);
      await refreshSession();
    }}

    async function refreshSession() {{
      if (!state.snapshot) return;
      const task = state.snapshot.tasks.find((item) => item.id === state.selectedTaskId);
      if (!task || !state.selectedSubagentId) {{
        renderSessionPayload(null, state.snapshot);
        return;
      }}
      try {{
        const payload = await fetchJson(`/api/session?task_id=${{encodeURIComponent(task.id)}}&subagent_id=${{encodeURIComponent(state.selectedSubagentId)}}`);
        renderSessionPayload(payload, state.snapshot);
      }} catch (error) {{
        renderSessionPayload(null, state.snapshot);
      }}
    }}

    function render(snapshot) {{
      renderQueue(snapshot);
      renderTask(snapshot);
    }}

    refreshSnapshot();
    setInterval(refreshSnapshot, __POLL_INTERVAL_MS__);
  </script>
</body>
</html>
"""
_INDEX_HTML = _INDEX_HTML.replace("__POLL_INTERVAL_MS__", str(_POLL_INTERVAL_MS)).replace(
    "__POLL_SECONDS__", f"{_POLL_INTERVAL_MS / 1000:.1f}"
)
