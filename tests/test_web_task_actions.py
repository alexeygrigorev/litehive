"""Tests for web dashboard task mutation endpoints."""

import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.workspace_helpers import *  # noqa: F401,F403

from litehive.web import LitehiveWebHandler


def _start_server(root: Path) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(LitehiveWebHandler, workspace_root=root),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def _post_json(base_url: str, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_web_task_action_endpoints_mutate_tasks(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    server, base_url = _start_server(tmp_path)
    try:
        status, created = _post_json(
            base_url,
            "/api/tasks",
            {
                "title": "Dashboard create",
                "goal": "Ship POST endpoints",
                "priority": "high",
                "engine": "codex",
                "acceptance_criteria": ["first criterion", "second criterion"],
            },
        )
        assert status == 201
        assert created["ok"] is True
        task_id = str(created["task_id"])
        created_task = get_task(tmp_path, task_id)
        assert created_task is not None
        assert created_task.title == "Dashboard create"
        assert created_task.goal == "Ship POST endpoints"
        assert created_task.priority == "high"
        assert created_task.engine == "codex"

        status, updated = _post_json(
            base_url,
            f"/api/tasks/{task_id}/update",
            {
                "goal": "Updated goal",
                "priority": "medium",
                "engine": "gemini",
                "acceptance_criteria": ["updated criterion"],
                "constraints": ["keep scope tight"],
                "plan_steps": ["wire endpoint", "add tests"],
            },
        )
        assert status == 200
        assert updated["ok"] is True
        updated_task = get_task(tmp_path, task_id)
        assert updated_task is not None
        assert updated_task.goal == "Updated goal"
        assert updated_task.priority == "medium"
        assert updated_task.engine == "gemini"
        assert updated_task.acceptance_criteria == ["updated criterion"]
        assert updated_task.constraints == ["keep scope tight"]
        assert updated_task.plan == ["wire endpoint", "add tests"]

        status, closed = _post_json(
            base_url,
            f"/api/tasks/{task_id}/close",
            {"outcome": "deferred", "reason": "Waiting on upstream"},
        )
        assert status == 200
        assert closed["ok"] is True
        closed_task = get_task(tmp_path, task_id)
        assert closed_task is not None
        assert closed_task.status == "deferred"
        assert closed_task.runtime.last_outcome.reason == "Waiting on upstream"

        status, requeued = _post_json(
            base_url,
            f"/api/tasks/{task_id}/requeue",
            {"front": True},
        )
        assert status == 200
        assert requeued["ok"] is True
        assert requeued["front"] is True
        assert requeued["queue_position"] == 1
        requeued_task = get_task(tmp_path, task_id)
        assert requeued_task is not None
        assert requeued_task.status == "queued"

        abandonable = create_task(tmp_path, title="Abandon me")
        close_task(tmp_path, abandonable.id, outcome="duplicate", reason="Already tracked")
        status, abandoned = _post_json(
            base_url,
            f"/api/tasks/{abandonable.id}/abandon",
            {},
        )
        assert status == 200
        assert abandoned["ok"] is True
        abandoned_task = get_task(tmp_path, abandonable.id)
        assert abandoned_task is not None
        assert abandoned_task.status == "cancelled"
        assert abandoned_task.runtime.execution_status == "cancelled"

        active = create_task(tmp_path, title="Stop me")
        set_active_task(tmp_path, active.id)
        status, stopped = _post_json(base_url, "/api/tasks/active/stop", {})
        assert status == 200
        assert stopped["ok"] is True
        assert stopped["task_id"] == active.id
        stopped_task = get_task(tmp_path, active.id)
        assert stopped_task is not None
        assert stopped_task.status == "parked"
        assert load_state(tmp_path).active_task_id is None
    finally:
        server.shutdown()
        server.server_close()


def test_web_task_action_endpoints_return_structured_errors(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    server, base_url = _start_server(tmp_path)
    try:
        status, missing_title = _post_json(base_url, "/api/tasks", {"goal": "No title"})
        assert status == 400
        assert missing_title == {"ok": False, "error": "'title' is required"}

        task = create_task(tmp_path, title="Close validation")
        status, invalid_outcome = _post_json(
            base_url,
            f"/api/tasks/{task.id}/close",
            {"outcome": "done"},
        )
        assert status == 400
        assert invalid_outcome["ok"] is False
        assert "Unsupported close outcome 'done'" in str(invalid_outcome["error"])

        status, missing_task = _post_json(
            base_url,
            "/api/tasks/T-9999/update",
            {"goal": "nope"},
        )
        assert status == 400
        assert missing_task == {"ok": False, "error": "Task T-9999 not found"}
    finally:
        server.shutdown()
        server.server_close()
