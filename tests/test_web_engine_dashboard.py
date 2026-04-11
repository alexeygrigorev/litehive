"""Tests for lock-free web dashboard snapshot reads."""

from contextlib import contextmanager
from functools import partial
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

from tests.workspace_helpers import (
    EngineUsageWindow,
    LitehiveConfig,
    _init_git_repo,
    create_task,
    ensure_workspace,
    load_config,
    load_state,
    pytest,
    save_state,
    save_task,
    save_task_runtime,
)

from litehive.tasks import require_task
from litehive.tasks.paths import runner_lock_path
from litehive.tasks.reports import load_task_thread
from litehive.web import (
    LitehiveWebHandler,
    WorkspaceStreamMonitor,
    read_engine_dashboard,
    switch_task_engine_via_web,
    update_default_engine,
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


def _install_normalized_quota_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "litehive.web.snapshot.check_codex_quota",
        lambda: SimpleNamespace(
            error=None,
            short_term=SimpleNamespace(percent_remaining=58.0, reset_at="2026-04-09T05:00:00Z"),
            long_term=SimpleNamespace(percent_remaining=39.0, reset_at="2026-04-15T00:00:00Z"),
        ),
    )
    monkeypatch.setattr(
        "litehive.web.snapshot.check_claude_quota",
        lambda: SimpleNamespace(
            error=None,
            short_term=SimpleNamespace(percent_remaining=62.5, reset_at="2026-04-09T04:00:00Z"),
            long_term=SimpleNamespace(percent_remaining=42.0, reset_at="2026-04-12T00:00:00Z"),
        ),
    )
    monkeypatch.setattr(
        "litehive.web.snapshot.check_copilot_quota",
        lambda: SimpleNamespace(
            error=None,
            short_term=SimpleNamespace(percent_remaining=100.0, reset_at=None),
            long_term=SimpleNamespace(percent_remaining=25.0, reset_at="2026-05-01T00:00:00Z"),
        ),
    )
    monkeypatch.setattr(
        "litehive.web.snapshot.check_zai_quota",
        lambda: SimpleNamespace(
            error=None,
            short_term=SimpleNamespace(percent_remaining=52.0, reset_at=None),
            long_term=SimpleNamespace(percent_remaining=100.0, reset_at=None),
        ),
    )


def test_read_engine_dashboard_includes_config_routing_and_monitoring(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="gemini",
            recovery_engine="codex",
            engine_preference=["gemini", "opencode", "codex"],
            engine_freeze={"codex": "2099-01-01T00:00:00Z"},
        ),
    )
    from litehive.models import EngineUsageRecord, WorkspaceEngineMonitoring
    from litehive.observability import save_engine_monitoring

    save_engine_monitoring(
        tmp_path,
        WorkspaceEngineMonitoring(
            engines={
                "gemini": EngineUsageRecord(
                    engine="gemini",
                    source="provider",
                    provider="google",
                    invocation_count=3,
                    success_count=2,
                    failure_count=1,
                    usage=EngineUsageWindow(used=1234, limit=4000, remaining=2766, unit="tokens"),
                    metadata={"prompt_tokens": 900, "completion_tokens": 334, "total_cost_usd": 12},
                )
            }
        ),
    )

    payload = read_engine_dashboard(tmp_path)

    assert payload["config"]["default_engine"] == "gemini"
    assert payload["config"]["active_engine_freezes"]["codex"] == "2099-01-01T00:00:00+00:00"
    assert payload["routing"]["default_fallback_order"] == ["gemini", "opencode", "codex"]
    assert payload["routing"]["task_types"][0]["effective_engine"] == "gemini"
    assert payload["monitoring"]["engines"]["gemini"]["usage"]["used"] == 1234
    assert payload["monitoring"]["engines"]["gemini"]["token_cost_fields"] == {
        "completion_tokens": 334,
        "prompt_tokens": 900,
        "total_cost_usd": 12,
    }


def test_read_engine_dashboard_includes_normalized_engine_quota(tmp_path: Path, monkeypatch) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    _install_normalized_quota_readers(monkeypatch)

    payload = read_engine_dashboard(tmp_path)
    quota = payload["quota"]["engines"]

    assert quota["codex"]["windows"] == [
        {
            "label": "short_term",
            "used_percent": 42.0,
            "remaining_percent": 58.0,
            "reset_at": "2026-04-09T05:00:00Z",
        },
        {
            "label": "long_term",
            "used_percent": 61.0,
            "remaining_percent": 39.0,
            "reset_at": "2026-04-15T00:00:00Z",
        },
    ]
    assert quota["claude"]["summary"] == "long_term 58% used"
    assert quota["copilot"]["windows"][1]["remaining_percent"] == 25.0
    assert quota["copilot"]["windows"][1]["reset_at"] == "2026-05-01T00:00:00Z"
    assert quota["goz"]["windows"][0]["remaining_percent"] == 52.0
    assert quota["opencode"]["windows"][1]["remaining_percent"] == 100.0


def test_read_engine_dashboard_marks_unavailable_quota_readers_fail_open(
    tmp_path: Path, monkeypatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)

    monkeypatch.setattr(
        "litehive.web.snapshot.check_codex_quota",
        lambda: SimpleNamespace(error="no auth token", short_term=None, long_term=None),
    )
    monkeypatch.setattr(
        "litehive.web.snapshot.check_claude_quota",
        lambda: SimpleNamespace(error="no-credentials", short_term=None, long_term=None),
    )
    monkeypatch.setattr(
        "litehive.web.snapshot.check_copilot_quota",
        lambda: SimpleNamespace(
            error="gh not on PATH",
            short_term=None,
            long_term=None,
        ),
    )
    monkeypatch.setattr(
        "litehive.web.snapshot.check_zai_quota",
        lambda: SimpleNamespace(error="goz not on PATH", short_term=None, long_term=None),
    )

    payload = read_engine_dashboard(tmp_path)
    quota = payload["quota"]["engines"]

    assert quota["codex"]["status"] == "unavailable"
    assert quota["codex"]["summary"] == "unavailable"
    assert quota["claude"]["error"] == "no-credentials"
    assert quota["copilot"]["windows"][0]["remaining_percent"] is None
    assert quota["goz"]["status"] == "unavailable"
    assert quota["opencode"]["error"] == "goz not on PATH"


def test_update_default_engine_persists_local_config(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    payload = update_default_engine(tmp_path, "gemini")
    config = load_config(tmp_path)

    assert payload["previous_default_engine"] == "codex"
    assert payload["default_engine"] == "gemini"
    assert payload["engines"]["config"]["default_engine"] == "gemini"
    assert config.default_engine == "gemini"


def test_switch_task_engine_via_web_reuses_switch_logic(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    task = create_task(tmp_path, title="Switch me", auto_commit=False)
    task.status = "parked"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    payload = switch_task_engine_via_web(
        tmp_path,
        task_id=task.id,
        engine="gemini",
        reason="Need larger context window",
    )
    reloaded = require_task(tmp_path, task.id)
    thread = load_task_thread(tmp_path, reloaded)

    assert payload["switch"]["previous_engine"] == "codex"
    assert payload["switch"]["new_engine"] == "gemini"
    assert "engine" not in payload["task"]["record"]
    assert reloaded.runtime.last_engine_switch is not None
    assert reloaded.runtime.last_engine_switch.to_engine == "gemini"
    assert thread[-1].message.startswith("Engine switch requested: Need larger context window")


def _seed_engine_monitoring(tmp_path: Path) -> None:
    from litehive.models import EngineUsageRecord, WorkspaceEngineMonitoring
    from litehive.observability import save_engine_monitoring

    save_engine_monitoring(
        tmp_path,
        WorkspaceEngineMonitoring(
            engines={
                "codex": EngineUsageRecord(
                    engine="codex",
                    invocation_count=2,
                    usage=EngineUsageWindow(used=50, limit=100, remaining=50, unit="percent"),
                    metadata={"prompt_tokens": 2000, "total_cost_usd": 4},
                )
            }
        ),
    )


def test_http_engines_endpoint_returns_dashboard_payload(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    task = create_task(tmp_path, title="HTTP switch", auto_commit=False)
    task.status = "parked"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)
    _seed_engine_monitoring(tmp_path)

    server, thread = _start_web_server(tmp_path)
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        conn.request("GET", "/api/engines")
        response = conn.getresponse()
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
        assert payload["config"]["default_engine"] == "codex"
        assert "quota" in payload
        assert payload["monitoring"]["engines"]["codex"]["token_cost_fields"]["prompt_tokens"] == 2000
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_default_engine_endpoint_persists_new_default(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    _seed_engine_monitoring(tmp_path)

    server, thread = _start_web_server(tmp_path)
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        conn.request(
            "POST",
            "/api/engines/default",
            body=json.dumps({"engine": "gemini"}),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
        assert payload["default_engine"] == "gemini"
        assert load_config(tmp_path).default_engine == "gemini"
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_task_engine_endpoint_switches_task_engine(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    task = create_task(tmp_path, title="HTTP switch", auto_commit=False)
    task.status = "parked"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    server, thread = _start_web_server(tmp_path)
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        conn.request(
            "POST",
            "/api/task/engine",
            body=json.dumps(
                {
                    "task_id": task.id,
                    "engine": "opencode",
                    "reason": "codex quota exhausted",
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
        assert "engine" not in payload["task"]["record"]
        assert payload["switch"]["new_engine"] == "opencode"
        assert require_task(tmp_path, task.id).runtime.last_engine_switch is not None
        assert require_task(tmp_path, task.id).runtime.last_engine_switch.to_engine == "opencode"
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _setup_reportable_task(tmp_path: Path):
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="HTTP review")
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "running"
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={"step": "testing", "status": "running", "started_at": "2026-04-08T12:00:00+00:00"}
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = [task.id]
    save_state(tmp_path, state)
    return task


def test_http_report_endpoint_advances_task_and_appends_thread(tmp_path: Path) -> None:
    task = _setup_reportable_task(tmp_path)

    server, thread = _start_web_server(tmp_path)
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        conn.request(
            "POST",
            "/api/report",
            body=json.dumps(
                {
                    "task_id": task.id,
                    "role": "qa",
                    "step": "testing",
                    "verdict": "pass",
                    "message": "Approved in browser.",
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
        assert payload["task"]["pipeline_status"] == "accepting"
        assert payload["task"]["thread"][-1]["message"] == "Approved in browser."
        assert require_task(tmp_path, task.id).pipeline_status == "accepting"
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_report_endpoint_requires_message_for_comment_verdict(tmp_path: Path) -> None:
    task = _setup_reportable_task(tmp_path)

    server, thread = _start_web_server(tmp_path)
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        conn.request(
            "POST",
            "/api/report",
            body=json.dumps(
                {
                    "task_id": task.id,
                    "role": "qa",
                    "step": "accepting",
                    "verdict": "comment",
                    "message": "",
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        assert response.status == 400
        payload = json.loads(response.read().decode("utf-8"))
        assert payload["error"] == "message is required"
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
