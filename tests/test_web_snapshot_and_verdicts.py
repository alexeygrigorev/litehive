"""Tests for lock-free web dashboard snapshot reads."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
    _init_git_repo,
    build_workspace_snapshot,
    create_task,
    ensure_workspace,
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
from litehive.tasks import require_task
from litehive.tasks.paths import runner_lock_path
from litehive.tasks.reports import append_thread_comment, load_task_thread
from litehive.workspace.locking import runner_status_readonly
from litehive.web import (
    LitehiveWebHandler,
    WorkspaceStreamMonitor,
    render_index,
    submit_stage_verdict_via_web,
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


def _create_detailed_snapshot_task(tmp_path: Path):
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Detailed task",
        goal="Ship the detail panel",
        acceptance_criteria=["criterion 1", "criterion 2"],
        priority="high",
    )
    task.constraints = ["keep scope tight"]
    task.plan = ["extend payload", "add editing"]
    task.depends_on = ["T-0009"]
    task.git.commit_sha = "abc123"
    task.git.checkpoint_base_sha = "def456"
    task.git.checkpoint_attempts = 2
    task.git.merge_agent_attempts = 1
    save_task(tmp_path, task)
    return task, task_dir(tmp_path, task)


def _iso8601_now_plus(*, minutes: int = 0, seconds: int = 0, days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days, minutes=minutes, seconds=seconds)).isoformat()


def _write_detailed_task_reports(base: Path, task_id: str) -> None:
    reports_dir = base / "reports"
    started_at = datetime.now(timezone.utc)
    for index in range(6):
        (reports_dir / f"implementing-{index + 1:03d}.yaml").write_text(
            yaml.safe_dump(
                {
                    "task_id": task_id,
                    "step": "implementing",
                    "verdict": "pass",
                    "summary": f"report {index + 1}",
                    "created_at": (started_at + timedelta(minutes=index)).isoformat(),
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )


def _write_detailed_task_recovery_and_thread(tmp_path: Path, task, base: Path) -> None:
    (base / "recovery").mkdir(parents=True, exist_ok=True)
    base_time = datetime.now(timezone.utc)
    (base / "recovery" / "recovery-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "stage": "implementing",
                "trigger": "stage_failure",
                "summary": "Recovered from a bad pass.",
                "runnable_state": "runnable",
                "created_at": (base_time + timedelta(hours=2)).isoformat(),
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
            created_at=base_time.isoformat(),
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
            created_at=(base_time + timedelta(hours=3)).isoformat(),
        ),
    )


def _write_detailed_task_events(base: Path, task_id: str) -> None:
    base_time = datetime.now(timezone.utc)
    (base / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": (base_time + timedelta(hours=4)).isoformat(),
                        "task_id": task_id,
                        "kind": "done",
                    }
                ),
                json.dumps(
                    {
                        "ts": base_time.isoformat(),
                        "task_id": task_id,
                        "kind": "queued",
                    }
                ),
                json.dumps(
                    {
                        "ts": (base_time + timedelta(hours=2)).isoformat(),
                        "task_id": task_id,
                        "kind": "started",
                    }
                ),
            ]
        ) + "\n",
        encoding="utf-8",
    )


def _snapshot_payload_for_task(tmp_path: Path, task_id: str) -> dict[str, object]:
    snapshot = build_workspace_snapshot(tmp_path)
    return next(item for item in snapshot["tasks"] if item["id"] == task_id)


def test_runner_status_readonly_returns_idle_when_no_metadata(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    status = runner_status_readonly(tmp_path)
    assert status.status == "idle"
    assert status.pid is None


def test_runner_status_readonly_returns_running_when_pid_alive(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    now = _iso8601_now_plus()
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
        "started_at": _iso8601_now_plus(minutes=-10),
        "heartbeat_at": _iso8601_now_plus(minutes=-9, seconds=-55),
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
        "started_at": _iso8601_now_plus(days=-1),
        "heartbeat_at": _iso8601_now_plus(days=-1),
    })
    status = runner_status_readonly(tmp_path)
    assert status.status == "late"


def test_build_workspace_snapshot_does_not_block_on_runner_lock(tmp_path: Path, monkeypatch) -> None:
    """Snapshot must complete even when fcntl.flock would block (simulated)."""
    now = _iso8601_now_plus()
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
    import litehive.workspace.locking as locking_mod

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


def test_build_workspace_snapshot_includes_task_record_metadata(tmp_path: Path) -> None:
    task, _ = _create_detailed_snapshot_task(tmp_path)
    payload = _snapshot_payload_for_task(tmp_path, task.id)

    assert payload["record"]["goal"] == "Ship the detail panel"
    assert payload["record"]["constraints"] == ["keep scope tight"]
    assert payload["record"]["plan"] == ["extend payload", "add editing"]
    assert payload["record"]["depends_on"] == ["T-0009"]
    assert payload["record"]["git"]["commit_sha"] == "abc123"
    assert payload["record"]["git"]["checkpoint_base_sha"] == "def456"
    assert payload["record"]["git"]["checkpoint_attempts"] == 2
    assert payload["record"]["git"]["merge_agent_attempts"] == 1


def test_build_workspace_snapshot_includes_task_report_history(tmp_path: Path) -> None:
    task, base = _create_detailed_snapshot_task(tmp_path)
    _write_detailed_task_reports(base, task.id)

    payload = _snapshot_payload_for_task(tmp_path, task.id)

    assert len(payload["reports"]) == 6
    assert payload["reports"][0]["summary"] == "report 1"
    assert payload["reports"][-1]["summary"] == "report 6"


def test_build_workspace_snapshot_includes_task_events_and_thread(tmp_path: Path) -> None:
    task, base = _create_detailed_snapshot_task(tmp_path)
    _write_detailed_task_recovery_and_thread(tmp_path, task, base)
    _write_detailed_task_events(base, task.id)

    payload = _snapshot_payload_for_task(tmp_path, task.id)

    assert [event["kind"] for event in payload["events"]] == ["queued", "started", "done"]
    assert [comment["role"] for comment in payload["thread"]] == ["planner", "qa"]


def test_build_workspace_snapshot_includes_recovery_reports_and_editable_fields(tmp_path: Path) -> None:
    task, base = _create_detailed_snapshot_task(tmp_path)
    _write_detailed_task_recovery_and_thread(tmp_path, task, base)

    snapshot = build_workspace_snapshot(tmp_path)
    payload = next(item for item in snapshot["tasks"] if item["id"] == task.id)

    assert payload["recovery_reports"][0]["summary"] == "Recovered from a bad pass."
    assert snapshot["editable_fields"]["priority_options"]


def test_render_index_includes_origin_metadata_sections() -> None:
    html = render_index()

    assert "Created From" in html
    assert "Upstream Origin" in html
    assert "GitHub Origin" in html
    assert "Engine Dashboard" in html
    assert "/api/engines" in html
    assert "/api/runner/status" in html
    assert "/api/queue/stop" in html
    assert "/api/report" in html
    assert "Submit Verdict" in html
    assert '["pass", "reject", "comment"]' in html
    assert '["pass", "fail", "reject", "blocked", "comment"]' not in html


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
        update={"step": "testing", "status": "running", "started_at": _iso8601_now_plus(minutes=-5)}
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
        update={"step": "accepting", "status": "running", "started_at": _iso8601_now_plus(minutes=-5)}
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


def test_submit_stage_verdict_via_web_legacy_fail_alias_requeues_task_for_implementation(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Legacy fail alias")
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "running"
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
        verdict="fail",
        message="Legacy fail should behave like reject.",
    )
    reloaded = require_task(tmp_path, task.id)
    thread = load_task_thread(tmp_path, reloaded)

    assert payload["submitted"]["verdict"] == "reject"
    assert reloaded.pipeline_status == "implementing"
    assert reloaded.status == "queued"
    assert thread[-1].verdict == "reject"


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
        },
    )["task"]

    reloaded = require_task(tmp_path, task.id)

    assert updated["record"]["goal"] == "Updated goal"
    assert updated["record"]["acceptance_criteria"] == ["one", "two"]
    assert updated["record"]["constraints"] == ["stay local"]
    assert updated["record"]["plan"] == ["edit", "verify"]
    assert updated["record"]["priority"] == "high"
    assert reloaded.goal == "Updated goal"
    assert reloaded.acceptance_criteria == ["one", "two"]
    assert reloaded.constraints == ["stay local"]
    assert reloaded.plan == ["edit", "verify"]
    assert reloaded.priority == "high"
