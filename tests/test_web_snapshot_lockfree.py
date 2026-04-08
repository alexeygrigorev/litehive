"""Tests for lock-free web dashboard snapshot reads."""

import json
import os
import time
from pathlib import Path

import yaml

from tests.workspace_helpers import *  # noqa: F401,F403

from litehive.models import GitHubOrigin, TaskThreadComment, TaskCreationSource, UpstreamContributionOrigin
from litehive.tasks import append_thread_comment, require_task
from litehive.tasks import runner_status_readonly
from litehive.tasks.paths import runner_lock_path
from litehive.web import _render_index, update_task_detail


def _write_runner_lock_metadata(root: Path, data: dict) -> None:
    lock_path = runner_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


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
