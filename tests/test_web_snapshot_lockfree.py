"""Tests for lock-free web dashboard snapshot reads."""

from contextlib import contextmanager
from functools import partial
import http.client
import json
import os
import time
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

from tests.workspace_helpers import (
    EngineUsageWindow,
    LitehiveConfig,
    RuntimeSubagentState,
    SubagentRef,
    _init_git_repo,
    build_workspace_snapshot,
    create_task,
    ensure_workspace,
    load_config,
    load_state,
    pytest,
    save_state,
    save_task,
    save_task_runtime,
    task_dir,
)

from litehive.models import (
    GitHubOrigin,
    TaskCreationSource,
    TaskThreadComment,
    UpstreamContributionOrigin,
)
from litehive.tasks import append_thread_comment, load_task_thread, require_task
from litehive.tasks import runner_status_readonly
from litehive.tasks.paths import runner_lock_path
from litehive.web import (
    LitehiveWebHandler,
    WorkspaceStreamMonitor,
    _render_index,
    read_engine_dashboard,
    submit_stage_verdict_via_web,
    switch_task_engine_via_web,
    update_task_detail,
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


def test_runner_status_readonly_returns_idle_when_no_metadata(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    status = runner_status_readonly(tmp_path)
    assert status.status == "idle"
    assert status.pid is None


def test_runner_status_readonly_returns_running_when_pid_alive(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    # Use our own PID — guaranteed alive.
    _write_runner_lock_metadata(tmp_path, {
        "status": "running",
        "pid": os.getpid(),
        "workspace": str(tmp_path),
        "command": "litehive run",
        "started_at": now,
        "heartbeat_at": now,
    })
    status = runner_status_readonly(tmp_path)
    assert status.status == "running"
    assert status.pid == os.getpid()


def test_runner_status_readonly_returns_stale_when_pid_dead(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    # Use a PID that almost certainly doesn't exist.
    _write_runner_lock_metadata(tmp_path, {
        "status": "running",
        "pid": 2_000_000_000,
        "workspace": str(tmp_path),
        "command": "litehive run",
        "started_at": "2026-04-08T10:00:00+00:00",
        "heartbeat_at": "2026-04-08T10:00:05+00:00",
    })
    status = runner_status_readonly(tmp_path)
    assert status.status == "stale"


def test_runner_status_readonly_returns_late_when_heartbeat_expired(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    # Heartbeat far in the past but PID alive.
    _write_runner_lock_metadata(tmp_path, {
        "status": "running",
        "pid": os.getpid(),
        "workspace": str(tmp_path),
        "command": "litehive run",
        "started_at": "2020-01-01T00:00:00+00:00",
        "heartbeat_at": "2020-01-01T00:00:00+00:00",
    })
    status = runner_status_readonly(tmp_path)
    assert status.status == "late"


def test_build_workspace_snapshot_does_not_block_on_runner_lock(tmp_path: Path, monkeypatch) -> None:
    """Snapshot must complete even when fcntl.flock would block (simulated)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Running task")
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = [task.id]
    save_state(tmp_path, state)

    # Write runner metadata with our own PID so readonly reports "running".
    _write_runner_lock_metadata(tmp_path, {
        "status": "running",
        "pid": os.getpid(),
        "workspace": str(tmp_path),
        "command": "litehive run --drain",
        "started_at": now,
        "heartbeat_at": now,
    })

    # Make fcntl.flock always raise BlockingIOError to prove the snapshot
    # code path never calls it.
    import litehive.tasks.locking as locking_mod

    def flock_that_blocks(fd, flags):
        raise BlockingIOError("flock must not be called from snapshot path")

    monkeypatch.setattr(locking_mod, "fcntl", type("FakeFcntl", (), {
        "flock": staticmethod(flock_that_blocks),
        "LOCK_EX": 2,
        "LOCK_NB": 4,
        "LOCK_UN": 8,
    })())

    start = time.monotonic()
    snapshot = build_workspace_snapshot(tmp_path)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"Snapshot took {elapsed:.1f}s — should be near-instant"
    assert snapshot["active_task_id"] == task.id
    assert snapshot["runner"]["status"] == "running"
    assert snapshot["runner"]["pid"] == os.getpid()
    assert len(snapshot["tasks"]) >= 1


def test_build_workspace_snapshot_includes_full_task_detail_payload(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Detailed task",
        goal="Ship the detail panel",
        acceptance_criteria=["criterion 1", "criterion 2"],
        priority="high",
        engine="codex",
    )
    task.constraints = ["keep scope tight"]
    task.plan = ["extend payload", "add editing"]
    task.depends_on = ["T-0009"]
    task.git.commit_sha = "abc123"
    task.git.checkpoint_base_sha = "def456"
    task.git.checkpoint_attempts = 2
    task.git.merge_agent_attempts = 1
    save_task(tmp_path, task)

    base = task_dir(tmp_path, task)
    reports_dir = base / "reports"
    for index in range(6):
        (reports_dir / f"implementing-{index + 1:03d}.yaml").write_text(
            yaml.safe_dump(
                {
                    "task_id": task.id,
                    "step": "implementing",
                    "verdict": "pass",
                    "summary": f"report {index + 1}",
                    "created_at": f"2026-04-08T10:0{index}:00+00:00",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    (base / "recovery").mkdir(parents=True, exist_ok=True)
    (base / "recovery" / "recovery-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "stage": "implementing",
                "trigger": "stage_failure",
                "summary": "Recovered from a bad pass.",
                "runnable_state": "runnable",
                "created_at": "2026-04-08T11:00:00+00:00",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    append_thread_comment(
        tmp_path,
        task,
        TaskThreadComment(
            role="planner",
            step="grooming",
            verdict="comment",
            message="Initial shaping.",
            created_at="2026-04-08T09:00:00+00:00",
        ),
    )
    append_thread_comment(
        tmp_path,
        task,
        TaskThreadComment(
            role="qa",
            step="testing",
            verdict="reject",
            message="Needs the full report history.",
            created_at="2026-04-08T12:00:00+00:00",
        ),
    )

    (base / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-04-08T12:00:00+00:00", "task_id": task.id, "kind": "done"}),
                json.dumps({"ts": "2026-04-08T08:00:00+00:00", "task_id": task.id, "kind": "queued"}),
                json.dumps({"ts": "2026-04-08T10:00:00+00:00", "task_id": task.id, "kind": "started"}),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    snapshot = build_workspace_snapshot(tmp_path)
    payload = next(item for item in snapshot["tasks"] if item["id"] == task.id)

    assert payload["record"]["goal"] == "Ship the detail panel"
    assert payload["record"]["constraints"] == ["keep scope tight"]
    assert payload["record"]["plan"] == ["extend payload", "add editing"]
    assert payload["record"]["depends_on"] == ["T-0009"]
    assert payload["record"]["git"]["commit_sha"] == "abc123"
    assert len(payload["reports"]) == 6
    assert [event["kind"] for event in payload["events"]] == ["queued", "started", "done"]
    assert [comment["role"] for comment in payload["thread"]] == ["planner", "qa"]
    assert payload["recovery_reports"][0]["summary"] == "Recovered from a bad pass."
    assert snapshot["editable_fields"]["priority_options"]
    assert snapshot["editable_fields"]["engine_options"]


def test_render_index_includes_origin_metadata_sections() -> None:
    html = _render_index()

    assert "Created From" in html
    assert "Upstream Origin" in html
    assert "GitHub Origin" in html
    assert "Engine Dashboard" in html
    assert "/api/engines" in html
    assert "/api/task/engine" in html
    assert "/api/report" in html
    assert "Submit Verdict" in html
    assert "blocked" in html


def test_build_workspace_snapshot_includes_origin_metadata_payload(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Origin task")
    task.created_from = TaskCreationSource(
        task_id="T-0007",
        stage="grooming",
        rationale="follow-up",
        blocking=True,
    )
    task.upstream_origin = UpstreamContributionOrigin(
        source_project="project-x",
        source_workspace=str(tmp_path.resolve()),
        source_task_id="T-0008",
        source_task_title="Upstream task",
        source_stage="accepting",
        source_role="reviewer",
        contribution_kind="runtime_bug",
        summary="Patch came from upstream.",
        details="Details for the imported work.",
        litehive_source_path=str((tmp_path / ".litehive").resolve()),
    )
    task.github_origin = GitHubOrigin(
        repo="owner/repo",
        issue_number=12,
        issue_url="https://github.com/owner/repo/issues/12",
    )
    save_task(tmp_path, task)

    snapshot = build_workspace_snapshot(tmp_path)
    payload = next(item for item in snapshot["tasks"] if item["id"] == task.id)

    assert payload["record"]["created_from"]["task_id"] == "T-0007"
    assert payload["record"]["created_from"]["blocking"] is True
    assert payload["record"]["upstream_origin"]["source_project"] == "project-x"
    assert payload["record"]["upstream_origin"]["source_task_id"] == "T-0008"
    assert payload["record"]["github_origin"]["repo"] == "owner/repo"
    assert payload["record"]["github_origin"]["issue_number"] == 12


def test_build_workspace_snapshot_marks_reviewable_active_task_for_verdict_form(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    reviewable = create_task(tmp_path, title="Reviewable task")
    reviewable.status = "in_progress"
    reviewable.pipeline_status = "testing"
    save_task(tmp_path, reviewable)

    inactive = create_task(tmp_path, title="Inactive review stage")
    inactive.status = "queued"
    inactive.pipeline_status = "accepting"
    save_task(tmp_path, inactive)

    state = load_state(tmp_path)
    state.active_task_id = reviewable.id
    state.queue = [reviewable.id, inactive.id]
    save_state(tmp_path, state)

    snapshot = build_workspace_snapshot(tmp_path)
    reviewable_payload = next(item for item in snapshot["tasks"] if item["id"] == reviewable.id)
    inactive_payload = next(item for item in snapshot["tasks"] if item["id"] == inactive.id)

    assert reviewable_payload["can_submit_verdict"] is True
    assert inactive_payload["can_submit_verdict"] is False


def test_submit_stage_verdict_via_web_advances_active_testing_task(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="QA review")
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "running"
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={"step": "testing", "status": "running", "started_at": "2026-04-08T10:00:00+00:00"}
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = [task.id]
    save_state(tmp_path, state)

    payload = submit_stage_verdict_via_web(
        tmp_path,
        task_id=task.id,
        role="qa",
        step="testing",
        verdict="pass",
        message="QA pass from the dashboard.",
    )
    reloaded = require_task(tmp_path, task.id)
    thread = load_task_thread(tmp_path, reloaded)
    refreshed_state = load_state(tmp_path)

    assert payload["task"]["pipeline_status"] == "accepting"
    assert payload["submitted"]["verdict"] == "pass"
    assert reloaded.pipeline_status == "accepting"
    assert reloaded.status == "in_progress"
    assert refreshed_state.active_task_id == task.id
    assert thread[-1].message == "QA pass from the dashboard."
    assert thread[-1].verdict == "pass"
    reports = sorted((task_dir(tmp_path, reloaded) / "reports").glob("testing-*.yaml"))
    assert len(reports) == 1
    report_payload = yaml.safe_load(reports[0].read_text(encoding="utf-8"))
    assert report_payload["verdict"] == "pass"
    assert report_payload["summary"] == "QA pass from the dashboard."


def test_submit_stage_verdict_via_web_reject_requeues_task_for_implementation(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Reviewer rejection")
    task.status = "in_progress"
    task.pipeline_status = "accepting"
    task.runtime.execution_status = "running"
    task.runtime.current_stage = task.runtime.current_stage.model_copy(
        update={"step": "accepting", "status": "running", "started_at": "2026-04-08T11:00:00+00:00"}
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = [task.id]
    save_state(tmp_path, state)

    payload = submit_stage_verdict_via_web(
        tmp_path,
        task_id=task.id,
        role="reviewer",
        step="accepting",
        verdict="reject",
        message="Needs one more implementation pass.",
    )
    reloaded = require_task(tmp_path, task.id)
    refreshed_state = load_state(tmp_path)

    assert payload["task"]["pipeline_status"] == "implementing"
    assert reloaded.pipeline_status == "implementing"
    assert reloaded.status == "queued"
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task.id


def test_submit_stage_verdict_via_web_rejects_invalid_request(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Wrong stage")
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = [task.id]
    save_state(tmp_path, state)

    with pytest.raises(ValueError, match="only available for active tasks in testing or accepting"):
        submit_stage_verdict_via_web(
            tmp_path,
            task_id=task.id,
            role="qa",
            step="implementing",
            verdict="pass",
            message="Should not be allowed.",
        )


def test_update_task_detail_persists_editable_fields(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Editable task")
    updated = update_task_detail(
        tmp_path,
        task.id,
        {
            "goal": "Updated goal",
            "acceptance_criteria": ["one", "two"],
            "constraints": ["stay local"],
            "plan": ["edit", "verify"],
            "priority": "high",
            "engine": "gemini",
        },
    )["task"]

    reloaded = require_task(tmp_path, task.id)

    assert updated["record"]["goal"] == "Updated goal"
    assert updated["record"]["acceptance_criteria"] == ["one", "two"]
    assert updated["record"]["constraints"] == ["stay local"]
    assert updated["record"]["plan"] == ["edit", "verify"]
    assert updated["record"]["priority"] == "high"
    assert updated["record"]["engine"] == "gemini"
    assert reloaded.goal == "Updated goal"
    assert reloaded.acceptance_criteria == ["one", "two"]
    assert reloaded.constraints == ["stay local"]
    assert reloaded.plan == ["edit", "verify"]
    assert reloaded.priority == "high"
    assert reloaded.engine == "gemini"


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
    task = create_task(tmp_path, title="Switch me", engine="codex", auto_commit=False)
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
    assert payload["task"]["record"]["engine"] == "gemini"
    assert reloaded.engine == "gemini"
    assert thread[-1].message.startswith("Engine switch requested: Need larger context window")


def test_http_engine_endpoints_and_task_switch(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    task = create_task(tmp_path, title="HTTP switch", engine="codex", auto_commit=False)
    task.status = "parked"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)
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

    server, thread = _start_web_server(tmp_path)
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        conn.request("GET", "/api/engines")
        response = conn.getresponse()
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
        assert payload["config"]["default_engine"] == "codex"
        assert payload["monitoring"]["engines"]["codex"]["token_cost_fields"]["prompt_tokens"] == 2000

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
        assert payload["task"]["record"]["engine"] == "opencode"
        assert payload["switch"]["new_engine"] == "opencode"
        assert require_task(tmp_path, task.id).engine == "opencode"
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_report_endpoint_updates_task_state_and_thread(tmp_path: Path) -> None:
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


def test_render_index_prefers_sse_with_polling_fallback() -> None:
    html = _render_index()

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


def test_sse_stream_pushes_live_session_artifact_updates(tmp_path: Path) -> None:
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
        yaml.safe_dump({"status": "running", "pid": 1234, "updated_at": "2026-04-08T18:45:00+00:00"}, sort_keys=False),
        encoding="utf-8",
    )
    (base / "transcript.md").write_text("initial transcript\n", encoding="utf-8")
    (base / "stdout.log").write_text("initial stdout\n", encoding="utf-8")
    (base / "stderr.log").write_text("", encoding="utf-8")

    server, thread = _start_web_server(tmp_path)
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        path = f"/api/stream?task_id={task.id}&subagent_id=SA-0001-swe"
        conn.request("GET", path)
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
    html = _render_index()

    assert "Move Up" in html
    assert "Move Down" in html
    assert "Promote" in html
    assert "/api/queue/move" in html
    assert "/api/queue/promote" in html
    assert "response.snapshot" in html
