import json
from pathlib import Path

from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.paths import workspace_path
from litehive.config.workspace import create_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.reports import StageReport
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import (
    WorkspaceTasks,
                )
from litehive.tasks.audit import load_task_audit_entries
from litehive.tasks.activity import task_activity_store_for_task
from litehive.workspace import Workspace
from litehive.tasks.event_log import (
    TaskEventLog,
)
from litehive.tasks.report_storage import TaskReportStore
from litehive.tasks.status import TaskStatusService
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
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Eventful task")

    TaskStatusService(workspace).update(task.id, title="Updated eventful task")
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

    TaskStatusService(workspace).close(task.id, outcome="duplicate", reason="same work")
    TaskStatusService(workspace).requeue(task.id, force=True)
    task = WorkspaceTasks(workspace).require(task.id)
    task.status = TaskStatus.DONE
    task.pipeline_status = PipelineStatus.DONE
    WorkspaceTasks(workspace).save(task)
    state = WorkspaceStateRepository(workspace).load()
    state.queue = [queued_id for queued_id in state.queue if queued_id != task.id]
    WorkspaceStateRepository(workspace).save(state)

    events, invalid = TaskEventLog(workspace).read()
    event_types = {str(event["event_type"]) for event in events if event.get("task_id") == task.id}

    assert invalid == 0
    assert TaskEventLog(workspace).path().exists()
    assert TaskEventLog(workspace).path() != workspace_path(tmp_path, "data.db")
    assert {
        "task_created",
        "task_updated",
        "task_reported",
        "task_closed",
        "task_requeued",
    } <= event_types
    assert all("timestamp" in event and "task_id" in event and "payload" in event for event in events)


def test_task_event_log_has_events_short_circuits_without_full_decode(tmp_path: Path) -> None:
    workspace = Workspace.from_path(tmp_path)
    path = TaskEventLog(workspace).path()
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

    assert TaskEventLog(workspace).has_events() is True


def test_db_rebuild_from_events_reconstructs_tasks_queue_activity_and_audit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    done = WorkspaceTasks(workspace).create( title="Replay done", goal="old goal")
    queued = WorkspaceTasks(workspace).create( title="Replay queued")
    TaskStatusService(workspace).update(
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
    TaskReportStore(workspace).record_stage_report(
        done,
        StageReport(
            task_id=done.id,
            pipeline_state="implementing",
            verdict="pass",
            summary="stage report replay",
            duration_seconds=12,
        ),
    )
    TaskStatusService(workspace).close(done.id, outcome="done", reason="verified")

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

    rebuilt_done = WorkspaceTasks(workspace).get(done.id)
    rebuilt_queued = WorkspaceTasks(workspace).get(queued.id)
    rebuilt_state = WorkspaceStateRepository(workspace).load()
    assert rebuilt_done is not None
    assert rebuilt_done.title == "Replay updated"
    assert rebuilt_done.priority == "high"
    assert rebuilt_done.goal == "new goal"
    assert rebuilt_done.status == "done"
    assert rebuilt_done.pipeline_status == "done"
    assert rebuilt_queued is not None
    assert rebuilt_queued.status == "queued"
    assert rebuilt_state.queue == [queued.id]
    assert [entry.message for entry in task_activity_store_for_task(workspace, rebuilt_done).load()] == ["ready to close"]
    replayed_stage_reports = TaskReportStore(workspace).load_stage_reports(rebuilt_done)
    assert len(replayed_stage_reports) == 1
    assert replayed_stage_reports[0].summary == "stage report replay"
    assert {"created", "metadata_updated", "closed"} <= {
        entry.action for entry in load_task_audit_entries(workspace, task_id=done.id, limit=10)
    }


def test_db_rebuild_from_events_refuses_incomplete_replay_source(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Do not silently drop me")
    TaskEventLog(workspace).path().unlink()

    result = CliRunner().invoke(
        app,
        ["db", "rebuild-from-events", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )

    assert result.return_value == 1
    assert "db rebuild-from-events failed: refusing event-log replay" in result.output
    assert WorkspaceTasks(workspace).get(task.id) is not None


def test_replay_skips_truncated_partial_log_record(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Partial replay")
    TaskEventLog(workspace).path().open("ab").write(b'{"schema_version":1,"event_type":')

    _clear_task_tables(tmp_path)
    summary = TaskEventLog(workspace).rebuild_sqlite()

    rebuilt = WorkspaceTasks(workspace).get(task.id)
    assert summary.invalid_events == 1
    assert rebuilt is not None
    assert rebuilt.title == "Partial replay"


def test_replay_keeps_discarded_created_task_removed(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Temporary import task")
    WorkspaceTasks(workspace).discard_created(task.id)

    assert WorkspaceTasks(workspace).get(task.id) is None

    _clear_task_tables(tmp_path)
    summary = TaskEventLog(workspace).rebuild_sqlite()

    events, invalid = TaskEventLog(workspace).read()
    assert invalid == 0
    assert [event["event_type"] for event in events if event.get("task_id") == task.id] == [
        "task_created",
        "task_removed",
    ]
    assert summary.tasks_rebuilt == 0
    assert WorkspaceTasks(workspace).get(task.id) is None
    assert [entry.action for entry in load_task_audit_entries(workspace, task_id=task.id, limit=10)] == [
        "removed",
        "created",
    ]


def test_create_workspace_rebuilds_after_database_loss_from_task_event_log(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="DB loss replay", acceptance_criteria=["restored from log"])
    event_log = TaskEventLog(workspace).path()
    first_event = json.loads(event_log.read_text(encoding="utf-8").splitlines()[0])

    _delete_workspace_db(tmp_path)
    create_workspace(tmp_path)

    rebuilt = WorkspaceTasks(workspace).get(task.id)
    rebuilt_state = WorkspaceStateRepository(workspace).load()
    assert event_log.exists()
    assert first_event["event_type"] == "task_created"
    assert rebuilt is not None
    assert rebuilt.title == "DB loss replay"
    assert rebuilt.acceptance_criteria == ["restored from log"]
    assert rebuilt_state.queue == [task.id]
