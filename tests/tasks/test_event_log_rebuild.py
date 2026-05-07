import json
from pathlib import Path

from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.paths import workspace_path
from litehive.config.workspace import create_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.reports import StageReport
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, discard_created_task, get_task, require_task, save_task
from litehive.tasks.audit import load_task_audit_entries
from litehive.workspace import Workspace
from litehive.tasks.event_log import (
    read_task_events,
    rebuild_sqlite_from_task_event_log,
    task_event_log_has_events,
    task_event_log_path,
)
from litehive.tasks.report_storage import load_stage_reports, record_stage_report
from litehive.tasks.status import close_task_for_workspace, requeue_task_for_workspace, update_task_for_workspace
from litehive.domain.common import PipelineStatus, TaskStatus


def _delete_workspace_db(root: Path) -> None:
    db_path = workspace_path(root, "data.db")
    for path in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        path.unlink(missing_ok=True)


def _clear_task_tables(root: Path) -> None:
    with connect_workspace_db(root) as connection:
        for table in (
            "task_state",
            "task_intent",
            "task_activity",
            "task_audit_log",
            "stage_reports",
            "recovery_reports",
            "pipeline_task_state",
            "pipeline_transitions",
            "pipeline_journal",
            "pool_state",
            "queue",
        ):
            connection.execute(f"DELETE FROM {table}")
        connection.commit()


def test_task_event_log_records_lifecycle_transition_types_outside_sqlite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Eventful task")

    update_task_for_workspace(Workspace.from_path(tmp_path), task.id, title="Updated eventful task")
    report = CliRunner().invoke(
        app,
        [
            "report",
            "--task-id",
            task.id,
            "--stage",
            "implementing",
            "--verdict",
            "pass",
            "--message",
            "implementation complete",
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )
    assert report.exit_code == 0, report.output

    close_task_for_workspace(Workspace.from_path(tmp_path), task.id, outcome="duplicate", reason="same work")
    requeue_task_for_workspace(Workspace.from_path(tmp_path), task.id, force=True)
    task = require_task(tmp_path, task.id)
    task.status = TaskStatus.DONE
    task.pipeline_status = PipelineStatus.DONE
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [queued_id for queued_id in state.queue if queued_id != task.id]
    save_state(tmp_path, state)

    events, invalid = read_task_events(Workspace.from_path(tmp_path))
    event_types = {str(event["event_type"]) for event in events if event.get("task_id") == task.id}

    assert invalid == 0
    assert task_event_log_path(Workspace.from_path(tmp_path)).exists()
    assert task_event_log_path(Workspace.from_path(tmp_path)) != workspace_path(tmp_path, "data.db")
    assert {
        "task_created",
        "task_updated",
        "task_reported",
        "task_closed",
        "task_requeued",
    } <= event_types
    assert all("timestamp" in event and "task_id" in event and "payload" in event for event in events)


def test_task_event_log_has_events_short_circuits_without_full_decode(tmp_path: Path) -> None:
    path = task_event_log_path(Workspace.from_path(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "{",
                json.dumps(
                    {
                        "schema_version": 1,
                        "timestamp": "2026-04-27T00:00:00+00:00",
                        "task_id": "T-0001",
                        "event_type": "task_created",
                        "payload": {},
                    }
                ),
                "{",
            ]
        ),
        encoding="utf-8",
    )

    assert task_event_log_has_events(Workspace.from_path(tmp_path)) is True


def test_db_rebuild_from_events_reconstructs_tasks_queue_activity_and_audit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    create_workspace(tmp_path)
    done = create_task(tmp_path, title="Replay done", goal="old goal")
    queued = create_task(tmp_path, title="Replay queued")
    update_task_for_workspace(
        Workspace.from_path(tmp_path),
        done.id,
        title="Replay updated",
        priority="high",
        goal="new goal",
    )

    report = CliRunner().invoke(
        app,
        [
            "report",
            "--task-id",
            done.id,
            "--stage",
            "implementing",
            "--verdict",
            "pass",
            "--message",
            "ready to close",
            "--workspace",
            str(tmp_path),
        ],
        standalone_mode=False,
    )
    assert report.exit_code == 0, report.output
    record_stage_report(Workspace.from_path(tmp_path),
        done,
        StageReport(
            task_id=done.id,
            pipeline_state="implementing",
            verdict="pass",
            summary="stage report replay",
            duration_seconds=12,
        ),
    )
    close_task_for_workspace(Workspace.from_path(tmp_path), done.id, outcome="done", reason="verified")

    _clear_task_tables(tmp_path)

    result = CliRunner().invoke(
        app,
        ["db", "rebuild-from-events", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert "events_replayed:" in result.output
    assert "tasks_rebuilt: 2" in result.output
    assert "queue_length: 1" in result.output

    rebuilt_done = get_task(tmp_path, done.id)
    rebuilt_queued = get_task(tmp_path, queued.id)
    rebuilt_state = load_state(tmp_path)
    assert rebuilt_done is not None
    assert rebuilt_done.title == "Replay updated"
    assert rebuilt_done.priority == "high"
    assert rebuilt_done.goal == "new goal"
    assert rebuilt_done.status == "done"
    assert rebuilt_done.pipeline_status == "done"
    assert rebuilt_queued is not None
    assert rebuilt_queued.status == "queued"
    assert rebuilt_state.queue == [queued.id]
    assert [entry.message for entry in Workspace.from_path(tmp_path).task_activity(rebuilt_done).load()] == ["ready to close"]
    replayed_stage_reports = load_stage_reports(Workspace.from_path(tmp_path), rebuilt_done)
    assert len(replayed_stage_reports) == 1
    assert replayed_stage_reports[0].summary == "stage report replay"
    assert {"created", "metadata_updated", "closed"} <= {
        entry.action for entry in load_task_audit_entries(Workspace.from_path(tmp_path), task_id=done.id, limit=10)
    }


def test_db_rebuild_from_events_refuses_incomplete_replay_source(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Do not silently drop me")
    task_event_log_path(Workspace.from_path(tmp_path)).unlink()

    result = CliRunner().invoke(
        app,
        ["db", "rebuild-from-events", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )

    assert result.return_value == 1
    assert "db rebuild-from-events failed: refusing event-log replay" in result.output
    assert get_task(tmp_path, task.id) is not None


def test_replay_skips_truncated_partial_log_record(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Partial replay")
    task_event_log_path(Workspace.from_path(tmp_path)).open("ab").write(b'{"schema_version":1,"event_type":')

    _clear_task_tables(tmp_path)
    summary = rebuild_sqlite_from_task_event_log(Workspace.from_path(tmp_path))

    rebuilt = get_task(tmp_path, task.id)
    assert summary.invalid_events == 1
    assert rebuilt is not None
    assert rebuilt.title == "Partial replay"


def test_replay_keeps_discarded_created_task_removed(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Temporary import task")
    discard_created_task(tmp_path, task.id)

    assert get_task(tmp_path, task.id) is None

    _clear_task_tables(tmp_path)
    summary = rebuild_sqlite_from_task_event_log(Workspace.from_path(tmp_path))

    events, invalid = read_task_events(Workspace.from_path(tmp_path))
    assert invalid == 0
    assert [event["event_type"] for event in events if event.get("task_id") == task.id] == [
        "task_created",
        "task_removed",
    ]
    assert summary.tasks_rebuilt == 0
    assert get_task(tmp_path, task.id) is None
    assert [entry.action for entry in load_task_audit_entries(Workspace.from_path(tmp_path), task_id=task.id, limit=10)] == [
        "removed",
        "created",
    ]


def test_create_workspace_rebuilds_after_database_loss_from_task_event_log(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="DB loss replay", acceptance_criteria=["restored from log"])
    event_log = task_event_log_path(Workspace.from_path(tmp_path))
    first_event = json.loads(event_log.read_text(encoding="utf-8").splitlines()[0])

    _delete_workspace_db(tmp_path)
    create_workspace(tmp_path)

    rebuilt = get_task(tmp_path, task.id)
    rebuilt_state = load_state(tmp_path)
    assert event_log.exists()
    assert first_event["event_type"] == "task_created"
    assert rebuilt is not None
    assert rebuilt.title == "DB loss replay"
    assert rebuilt.acceptance_criteria == ["restored from log"]
    assert rebuilt_state.queue == [task.id]
