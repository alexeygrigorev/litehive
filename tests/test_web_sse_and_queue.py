"""Tests for lock-free web dashboard snapshot reads."""

from contextlib import contextmanager
from functools import partial
import http.client
import json
import time
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

from tests.workspace_helpers import (
    RuntimeSubagentState,
    SubagentRef,
    _init_git_repo,
    create_task,
    ensure_workspace,
    get_task,
    load_state,
    save_state,
    save_task,
    save_task_runtime,
    task_dir,
)

from litehive.tasks.paths import runner_lock_path
from litehive.web import (
    LitehiveWebHandler,
    WorkspaceStreamMonitor,
    render_index,
    update_task_detail,
)


def _write_runner_lock_metadata(root: Path, data: dict) -> None:
    lock_path = runner_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _start_web_server(root: Path) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(LitehiveWebHandler, workspace_root=root))
    server.workspace_stream_monitor = WorkspaceStreamMonitor(root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _read_sse_event(response: http.client.HTTPResponse) -> tuple[str, dict[str, object]]:
    event_name = ""
    data: dict[str, object] | None = None
    while True:
        line = response.readline().decode("utf-8")
        assert line, "SSE stream closed unexpectedly"
        stripped = line.rstrip("\n")
        if not stripped:
            if event_name:
                return event_name, data or {}
            continue
        if stripped.startswith(":") or stripped.startswith("retry:"):
            continue
        if stripped.startswith("event: "):
            event_name = stripped.removeprefix("event: ")
            continue
        if stripped.startswith("data: "):
            data = json.loads(stripped.removeprefix("data: "))
            continue


@contextmanager
def _serve_web(root: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(LitehiveWebHandler, workspace_root=root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _post_json_error(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_render_index_prefers_sse_with_polling_fallback() -> None:
    html = render_index()

    assert "EventSource" in html
    assert "/api/stream" in html
    assert "startPolling" in html


def test_sse_stream_emits_initial_and_changed_snapshot_only(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Streamed task")
    server, thread = _start_web_server(tmp_path)
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        conn.request("GET", "/api/stream")
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"

        event_name, payload = _read_sse_event(response)
        assert event_name == "snapshot"
        assert payload["kind"] == "initial"
        assert payload["snapshot"]["tasks"][0]["id"] == task.id

        update_task_detail(tmp_path, task.id, {"goal": "updated via sse"})

        event_name, payload = _read_sse_event(response)
        assert event_name == "snapshot"
        assert payload["kind"] == "update"
        assert payload["diff"]["changed_task_ids"] == [task.id]
        changed = next(item for item in payload["snapshot"]["tasks"] if item["id"] == task.id)
        assert changed["record"]["goal"] == "updated via sse"
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _assert_sse_stream_pushes_live_session_artifact_updates(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Session stream")
    task.subagents = [
        SubagentRef(
            id="SA-0001-swe",
            role="swe",
            engine="codex",
            status="running",
            path="subagents/SA-0001-swe",
        )
    ]
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001-swe",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-0001-swe",
        started_at="2026-04-08T18:45:00+00:00",
        updated_at="2026-04-08T18:45:00+00:00",
        pid=1234,
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    base.mkdir(parents=True, exist_ok=True)
    (base / "session.yaml").write_text(
        yaml.safe_dump(
            {"status": "running", "pid": 1234, "updated_at": "2026-04-08T18:45:00+00:00"},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (base / "transcript.md").write_text("initial transcript\n", encoding="utf-8")
    (base / "stdout.log").write_text("initial stdout\n", encoding="utf-8")
    (base / "stderr.log").write_text("", encoding="utf-8")

    server, thread = _start_web_server(tmp_path)
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        conn.request("GET", f"/api/stream?task_id={task.id}&subagent_id=SA-0001-swe")
        response = conn.getresponse()
        assert response.status == 200

        event_name, payload = _read_sse_event(response)
        assert event_name == "snapshot"
        event_name, payload = _read_sse_event(response)
        assert event_name == "session"
        assert payload["session"]["artifacts"][1]["content"] == "initial stdout\n"

        (base / "stdout.log").write_text("initial stdout\nsecond line\n", encoding="utf-8")
        (base / "stderr.log").write_text("stderr line\n", encoding="utf-8")

        event_name, payload = _read_sse_event(response)
        if event_name == "snapshot":
            event_name, payload = _read_sse_event(response)
        assert event_name == "session"
        assert sorted(payload["diff"]["changed_artifacts"]) == ["stderr", "stdout"]
        assert "second line" in payload["session"]["artifacts"][1]["content"]
        assert payload["session"]["artifacts"][2]["content"] == "stderr line\n"
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_sse_stream_pushes_live_session_artifact_updates(tmp_path: Path) -> None:
    _assert_sse_stream_pushes_live_session_artifact_updates(tmp_path)


def test_sse_idle_client_waits_without_rebuilding_snapshots(tmp_path: Path, monkeypatch) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Idle stream")

    import litehive.web as web_module

    calls = 0
    original = web_module.build_workspace_snapshot

    def counting_snapshot(root: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return original(root)

    monkeypatch.setattr(web_module, "build_workspace_snapshot", counting_snapshot)

    server, thread = _start_web_server(tmp_path)
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        conn.request("GET", "/api/stream")
        response = conn.getresponse()
        assert response.status == 200
        event_name, _payload = _read_sse_event(response)
        assert event_name == "snapshot"
        first_count = calls
        time.sleep(0.8)
        assert calls == first_count == 1
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_queue_move_endpoint_moves_task_and_returns_updated_snapshot(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    third = create_task(tmp_path, title="Third task")

    with _serve_web(tmp_path) as base_url:
        status, payload = _post_json(
            f"{base_url}/api/queue/move",
            {"task_id": third.id, "position": 2},
        )

    assert status == 200
    assert payload["ok"] is True
    assert payload["queue"] == [first.id, third.id, second.id]
    assert payload["snapshot"]["queue"] == [first.id, third.id, second.id]
    assert load_state(tmp_path).queue == [first.id, third.id, second.id]


def test_queue_promote_endpoint_resumes_flagged_task_to_front(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Resume me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [first.id]
    save_state(tmp_path, state)

    with _serve_web(tmp_path) as base_url:
        status, payload = _post_json(
            f"{base_url}/api/queue/promote",
            {"task_id": flagged.id},
        )

    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert status == 200
    assert payload["ok"] is True
    assert payload["queue"] == [flagged.id, first.id]
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "testing"
    assert resumed.runtime.execution_status == "idle"
    assert load_state(tmp_path).queue == [flagged.id, first.id]


def test_queue_prioritize_endpoint_reorders_tasks_and_rejects_duplicates(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    third = create_task(tmp_path, title="Third task")

    with _serve_web(tmp_path) as base_url:
        status, payload = _post_json(
            f"{base_url}/api/queue/prioritize",
            {"task_ids": [third.id, first.id]},
        )
        error_status, error_payload = _post_json_error(
            f"{base_url}/api/queue/prioritize",
            {"task_ids": [second.id, second.id]},
        )

    assert status == 200
    assert payload["ok"] is True
    assert payload["queue"] == [third.id, first.id, second.id]
    assert error_status == 400
    assert error_payload["error"] == f"Task ids must be unique: {second.id}"
    assert load_state(tmp_path).queue == [third.id, first.id, second.id]


def test_render_index_includes_queue_controls_and_refresh_wiring() -> None:
    html = render_index()

    assert "Move Up" in html
    assert "Move Down" in html
    assert "Promote" in html
    assert "/api/queue/move" in html
    assert "/api/queue/promote" in html
    assert "response.snapshot" in html
