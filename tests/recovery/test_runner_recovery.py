import json
from pathlib import Path

import pytest

from litehive.agents.session_store import load_subagent_report, load_subagent_session, save_subagent_artifacts
from litehive.config.workspace import ensure_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.runtime import RuntimeSubagentState
from litehive.recovery.execution_recovery import prepare_interrupted_task, recover_stale_runner_state
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, get_task, save_task


def _seed_running_task(tmp_path: Path, *, stage: str, active: bool) -> tuple[str, str]:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title=f"{stage} recovery")
    task.status = "in_progress"
    task.pipeline_status = stage
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.current_stage.stage = stage
    task.runtime.current_stage.status = "running"
    task.runtime.current_stage.started_at = "2026-04-12T10:00:00Z"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id if active else None
    save_state(tmp_path, state)
    return task.id, stage


def _rewrite_runtime_payload_as_legacy_flat(root: Path, task_id: str) -> None:
    with connect_workspace_db(root) as connection:
        row = connection.execute(
            "SELECT payload, updated_at FROM task_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        runtime = payload["runtime"]
        flat_runtime = {
            **runtime.get("pipeline", {}),
            **runtime.get("execution", {}),
        }
        payload["runtime"] = flat_runtime
        connection.execute(
            "UPDATE task_state SET payload = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(payload, sort_keys=True), row["updated_at"], task_id),
        )
        connection.commit()


def _task_state_runtime_payload(root: Path, task_id: str) -> dict:
    with connect_workspace_db(root) as connection:
        row = connection.execute("SELECT payload FROM task_state WHERE task_id = ?", (task_id,)).fetchone()
    assert row is not None
    return json.loads(row["payload"])["runtime"]


@pytest.mark.parametrize(
    ("stage", "active"),
    [("implementing", True), ("commit_to_git", True), ("implementing", False)],
)
def test_recover_stale_runner_state_requeues_running_task(tmp_path: Path, stage: str, active: bool) -> None:
    task_id, expected_stage = _seed_running_task(tmp_path, stage=stage, active=active)

    assert recover_stale_runner_state(tmp_path) is True

    refreshed = get_task(tmp_path, task_id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == expected_stage
    assert refreshed.runtime.execution_status == "idle"
    assert refreshed.runtime.current_stage.stage == expected_stage
    assert refreshed.runtime.current_stage.status == "idle"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.stage == expected_stage
    assert refreshed.runtime.interruption.resume_stage == expected_stage

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task_id


def test_recover_stale_runner_state_requeues_legacy_flat_running_runtime(tmp_path: Path) -> None:
    task_id, expected_stage = _seed_running_task(tmp_path, stage="implementing", active=True)
    _rewrite_runtime_payload_as_legacy_flat(tmp_path, task_id)

    assert recover_stale_runner_state(tmp_path) is True

    refreshed = get_task(tmp_path, task_id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == expected_stage
    assert refreshed.runtime.execution_status == "idle"
    assert refreshed.runtime.current_stage.stage == expected_stage
    assert refreshed.runtime.current_stage.status == "idle"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.stage == expected_stage
    assert refreshed.runtime.interruption.resume_stage == expected_stage
    assert refreshed.runtime.last_outcome.reason_code == "execution_interrupted"

    raw_payload = _task_state_runtime_payload(tmp_path, task_id)
    assert set(raw_payload) == {"pipeline", "execution"}
    assert "execution_status" not in raw_payload
    assert raw_payload["pipeline"]["execution_status"] == "idle"
    assert raw_payload["execution"]["interruption"]["resume_stage"] == expected_stage

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task_id


def test_recover_stale_runner_state_preserves_runtime_stage_when_pipeline_status_degraded(
    tmp_path: Path,
) -> None:
    task_id, _ = _seed_running_task(tmp_path, stage="testing", active=True)
    task = get_task(tmp_path, task_id)
    assert task is not None
    task.pipeline_status = "backlog"
    save_task(tmp_path, task)

    assert recover_stale_runner_state(tmp_path) is True

    refreshed = get_task(tmp_path, task_id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "idle"
    assert refreshed.runtime.current_stage.stage == "testing"
    assert refreshed.runtime.current_stage.status == "idle"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.resume_stage == "testing"

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task_id


def test_recover_stale_runner_state_canonicalizes_nonrunning_stranded_task(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Stranded non-running task",
        acceptance_criteria=["resume from testing"],
    )
    task.status = "in_progress"
    task.pipeline_status = "backlog"
    task.runtime.execution_status = "idle"
    task.runtime.current_stage.stage = "testing"
    task.runtime.current_stage.status = "idle"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = []
    save_state(tmp_path, state)

    assert recover_stale_runner_state(tmp_path) is True

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "idle"
    assert refreshed.runtime.current_stage.stage == "testing"
    assert refreshed.runtime.current_stage.status == "idle"

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task.id


def test_recover_stale_runner_state_canonicalizes_queued_interrupted_marker(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Queued interrupted task",
        acceptance_criteria=["resume from grooming"],
    )
    task.status = "queued"
    task.pipeline_status = "grooming"
    task.runtime.execution_status = "interrupted"
    task.runtime.current_stage.stage = "grooming"
    task.runtime.current_stage.status = "running"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [task.id]
    save_state(tmp_path, state)

    assert recover_stale_runner_state(tmp_path) is True

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "grooming"
    assert refreshed.runtime.execution_status == "idle"
    assert refreshed.runtime.current_stage.stage == "grooming"
    assert refreshed.runtime.current_stage.status == "idle"

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue == [task.id]


def test_recover_stale_runner_state_clears_non_running_active_task_id(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Flagged but not running")
    task.status = "flagged"
    task.pipeline_status = "flagged"
    task.runtime.execution_status = "idle"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    assert recover_stale_runner_state(tmp_path) is True
    assert get_task(tmp_path, task.id).runtime.execution_status == "idle"  # type: ignore[union-attr]
    assert load_state(tmp_path).active_task_id is None


def test_prepare_interrupted_task_writes_resume_bookkeeping(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Interrupted run")
    subagent_path = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "subagents" / "SA-1234-swe"
    subagent_path.mkdir(parents=True)
    save_subagent_artifacts(
        tmp_path,
        task.id,
        "SA-1234",
        session={"status": "running"},
        report={"summary": "finished half the change"},
    )

    task.status = "in_progress"
    task.pipeline_status = "implementing"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.current_stage.stage = "implementing"
    task.runtime.current_stage.status = "running"
    task.runtime.current_stage.started_at = "2026-04-12T10:00:00Z"
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-1234",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-1234-swe",
        sandboxed=False,
        sandbox_summary="host",
        started_at="2026-04-12T10:00:00Z",
        updated_at="2026-04-12T10:05:00Z",
    )

    prepare_interrupted_task(
        tmp_path,
        task,
        stage="implementing",
        summary="Interrupted run recovered. Resume from `implementing`.",
        reason="received ctrl-c",
    )

    assert task.runtime.interruption is not None
    assert task.runtime.interruption.reason == "received ctrl-c"
    assert task.runtime.interruption.resume_stage == "implementing"
    assert task.runtime.active_subagent is None
    assert task.runtime.interruption.subagent is not None
    assert task.runtime.interruption.subagent.status == "interrupted"

    session = load_subagent_session(tmp_path, task.id, "SA-1234")
    report = load_subagent_report(tmp_path, task.id, "SA-1234")
    assert session["status"] == report["status"] == "interrupted"
    assert session["resume_stage"] == report["resume_stage"] == "implementing"
    assert session["interruption_reason"] == report["interruption_reason"] == "received ctrl-c"
