from tests.workspace_helpers import *  # noqa: F401,F403

from functools import partial
from http.server import ThreadingHTTPServer
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from litehive.web import LitehiveWebHandler, build_daemon_status_payload


def _start_web_server(workspace: Path) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(LitehiveWebHandler, workspace_root=workspace),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _read_json(base_url: str, path: str, *, method: str = "GET") -> tuple[int, dict[str, object]]:
    request = Request(f"{base_url}{path}", method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_build_daemon_status_payload_includes_uptime_and_recent_logs(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    started_at = "2026-04-08T10:00:00+00:00"
    monkeypatch.setattr(
        "litehive.web.get_workspace_daemon",
        lambda root: {
            "workspace": str(root.resolve()),
            "pid": 4321,
            "started_at": started_at,
            "log_dir": str(root / ".litehive" / "logs" / "run-all" / "current"),
        },
    )
    monkeypatch.setattr(
        "litehive.web.list_recent_run_all_logs",
        lambda root: [
            {
                "name": "20260408T100000Z",
                "path": ".litehive/logs/run-all/20260408T100000Z",
                "modified_at": "2026-04-08T10:02:00+00:00",
                "files": [
                    {
                        "name": "0001-run.log",
                        "path": ".litehive/logs/run-all/20260408T100000Z/0001-run.log",
                        "size": 7,
                        "modified_at": "2026-04-08T10:02:00+00:00",
                        "preview": "hello\n",
                        "truncated": False,
                    }
                ],
            }
        ],
    )

    payload = build_daemon_status_payload(tmp_path)

    assert payload["daemon_status"] == "running"
    assert payload["pid"] == 4321
    assert payload["started_at"] == started_at
    assert payload["uptime_seconds"] is not None
    assert payload["recent_logs"][0]["files"][0]["preview"] == "hello\n"


def test_web_daemon_api_supports_status_start_stop_restart_and_dashboard_markup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    daemon_state: dict[str, object] = {}
    pid_counter = {"value": 5000}

    def fake_get_workspace_daemon(root: Path) -> dict[str, object] | None:
        return dict(daemon_state) if daemon_state else None

    def fake_start_background_daemon(root: Path) -> int:
        pid_counter["value"] += 1
        pid = pid_counter["value"]
        daemon_state.clear()
        daemon_state.update(
            {
                "workspace": str(root.resolve()),
                "pid": pid,
                "started_at": "2026-04-08T10:00:00+00:00",
                "log_dir": str(root / ".litehive" / "logs" / "run-all" / "current"),
            }
        )
        return pid

    def fake_stop_workspace_daemon(root: Path) -> dict[str, object] | None:
        if not daemon_state:
            return None
        previous = dict(daemon_state)
        daemon_state.clear()
        return previous

    monkeypatch.setattr("litehive.web.get_workspace_daemon", fake_get_workspace_daemon)
    monkeypatch.setattr("litehive.web.start_background_daemon", fake_start_background_daemon)
    monkeypatch.setattr("litehive.web.stop_workspace_daemon", fake_stop_workspace_daemon)
    monkeypatch.setattr(
        "litehive.web.list_recent_run_all_logs",
        lambda root: [
            {
                "name": "20260408T100000Z",
                "path": ".litehive/logs/run-all/20260408T100000Z",
                "modified_at": "2026-04-08T10:02:00+00:00",
                "files": [
                    {
                        "name": "0001-run.log",
                        "path": ".litehive/logs/run-all/20260408T100000Z/0001-run.log",
                        "size": 14,
                        "modified_at": "2026-04-08T10:02:00+00:00",
                        "preview": "tasks_run: 1\n",
                        "truncated": False,
                    }
                ],
            }
        ],
    )

    server, thread = _start_web_server(tmp_path)
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base_url}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        assert 'id="daemon-start"' in html
        assert 'id="daemon-stop"' in html
        assert 'id="daemon-restart"' in html
        assert "/api/daemon/status" in html
        assert 'id="daemon-log-viewer"' in html

        status_code, payload = _read_json(base_url, "/api/daemon/status")
        assert status_code == 200
        assert payload["daemon_status"] == "stopped"
        assert payload["pid"] is None

        status_code, payload = _read_json(base_url, "/api/daemon/start", method="POST")
        assert status_code == 202
        assert payload["daemon_status"] == "running"
        first_pid = payload["pid"]

        status_code, payload = _read_json(base_url, "/api/daemon/status")
        assert status_code == 200
        assert payload["daemon_status"] == "running"
        assert payload["pid"] == first_pid
        assert payload["recent_logs"][0]["files"][0]["preview"] == "tasks_run: 1\n"

        status_code, payload = _read_json(base_url, "/api/daemon/restart", method="POST")
        assert status_code == 202
        assert payload["daemon_status"] == "running"
        assert payload["previous_pid"] == first_pid
        assert payload["pid"] != first_pid
        second_pid = payload["pid"]

        status_code, payload = _read_json(base_url, "/api/daemon/stop", method="POST")
        assert status_code == 200
        assert payload["daemon_status"] == "stopped"
        assert payload["previous_pid"] == second_pid
        assert payload["pid"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_web_daemon_api_returns_json_error_when_stop_requested_while_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    monkeypatch.setattr("litehive.web.get_workspace_daemon", lambda root: None)
    monkeypatch.setattr("litehive.web.stop_workspace_daemon", lambda root: None)

    server, thread = _start_web_server(tmp_path)
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status_code, payload = _read_json(base_url, "/api/daemon/stop", method="POST")
        assert status_code == 409
        assert payload == {"error": "daemon is not running"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
