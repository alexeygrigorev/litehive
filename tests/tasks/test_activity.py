import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from litehive.config.workspace import create_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.agent import SubagentId
from litehive.domain.common import Verdict
from litehive.domain.reports import TaskActivityEntry
from litehive.state.records import WorkspaceTasks
from litehive.tasks.activity import task_activity_store_for_task
from litehive.tasks.activity_rendering import append_activity_entry
from litehive.workspace import Workspace


def _activity_rows(root: Path, task_id: str) -> list[dict]:
    with connect_workspace_db(root) as connection:
        rows = connection.execute(
            """
            SELECT entry_index, payload
            FROM task_activity
            WHERE task_id = ?
            ORDER BY entry_index
            """,
            (task_id,),
        ).fetchall()
    return [{"entry_index": row["entry_index"], "payload": json.loads(row["payload"])} for row in rows]


def test_append_activity_entry_persists_to_sqlite(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Activity")

    append_activity_entry(
        Workspace.from_path(tmp_path),
        task,
        TaskActivityEntry(
            source="agent",
            role="swe",
            stage="implementing",
            verdict="pass",
            message="new",
            source_subagent_id="SA-0001",
        ),
    )

    assert [entry.message for entry in task_activity_store_for_task(Workspace.from_path(tmp_path), task).load()] == ["new"]
    rows = _activity_rows(tmp_path, task.id)
    assert len(rows) == 1
    assert rows[0]["entry_index"] == 0
    assert rows[0]["payload"]["role"] == "swe"
    assert rows[0]["payload"]["source"] == "agent"
    assert rows[0]["payload"]["stage"] == "implementing"
    assert rows[0]["payload"]["verdict"] == "pass"
    assert rows[0]["payload"]["message"] == "new"
    assert rows[0]["payload"]["created_at"]


def test_task_activity_entry_carries_verdict_as_domain_enum(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Typed verdict")
    workspace = Workspace.from_path(tmp_path)

    task_activity_store_for_task(workspace, task).append(
        TaskActivityEntry(
            source="agent",
            role="swe",
            stage="implementing",
            verdict="reject",
            message="typed",
            source_subagent_id="SA-0001",
        )
    )

    loaded = task_activity_store_for_task(workspace, task).load()

    assert loaded[0].verdict is Verdict.REJECT
    assert _activity_rows(tmp_path, task.id)[0]["payload"]["verdict"] == "reject"


def test_task_activity_log_latest_returns_newest_entry(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Latest activity")
    workspace = Workspace.from_path(tmp_path)
    activity = task_activity_store_for_task(workspace, task)

    activity.append(
        TaskActivityEntry(source="operator", role="operator", stage="backlog", verdict="comment", message="first")
    )
    activity.append(
        TaskActivityEntry(source="operator", role="operator", stage="backlog", verdict="comment", message="second")
    )

    latest = activity.latest()

    assert latest is not None
    assert latest.message == "second"


def test_load_task_activity_ignores_stale_filesystem_activity(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create(title="SQLite only")
    source_dir = workspace.task_dir(task)
    (source_dir / ("comments" + ".yaml")).write_text("- message: stale mirror\n", encoding="utf-8")
    (source_dir / ("thread" + ".yaml")).write_text("- message: stale legacy\n", encoding="utf-8")

    append_activity_entry(
        workspace,
        task,
        TaskActivityEntry(role="qa", stage="testing", verdict="comment", message="canonical db entry"),
    )

    assert [entry.message for entry in task_activity_store_for_task(workspace, task).load()] == ["canonical db entry"]


def test_task_activity_entry_models_non_agent_sources(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Operator activity")

    append_activity_entry(
        Workspace.from_path(tmp_path),
        task,
        TaskActivityEntry(
            source="operator",
            role="operator",
            stage="backlog",
            verdict="comment",
            message="operator switched engine",
        ),
    )

    loaded = task_activity_store_for_task(Workspace.from_path(tmp_path), task).load()

    assert loaded[0].source == "operator"
    assert loaded[0].source_subagent_id is None


def test_workspace_task_activity_returns_latest_matching_entry(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Activity query")
    workspace = Workspace.from_path(tmp_path)

    append_activity_entry(
        workspace,
        task,
        TaskActivityEntry(
            source="agent",
            role="swe",
            stage="implementing",
            verdict="pass",
            message="older swe report",
            source_subagent_id="SA-0001",
        ),
    )
    append_activity_entry(
        workspace,
        task,
        TaskActivityEntry(
            source="agent",
            role="swe",
            stage="implementing",
            verdict="reject",
            message="newer swe report",
            source_subagent_id="SA-0002",
        ),
    )

    latest = task_activity_store_for_task(workspace, task).latest_entry(
        role="swe",
        stage="implementing",
        source_subagent_id=SubagentId("SA-0001"),
        verdicts={"pass", "reject"},
    )

    assert latest is not None
    assert latest.message == "older swe report"


def test_task_activity_entry_requires_subagent_id_for_agent_source() -> None:
    with pytest.raises(ValidationError, match="agent activity requires source_subagent_id"):
        TaskActivityEntry(
            source="agent",
            role="swe",
            stage="implementing",
            verdict="pass",
            message="missing session",
        )

    operator_entry = TaskActivityEntry(
        source="operator",
        role="operator",
        stage="backlog",
        verdict="comment",
        message="operator note",
    )

    assert operator_entry.source_subagent_id is None


def test_task_activity_entry_infers_legacy_source_from_subagent_id() -> None:
    legacy_agent_entry = TaskActivityEntry(
        role="swe",
        stage="implementing",
        verdict="pass",
        message="legacy agent row",
        source_subagent_id="SA-0001",
    )
    legacy_operator_entry = TaskActivityEntry(
        role="operator",
        stage="backlog",
        verdict="comment",
        message="legacy operator row",
    )

    assert legacy_agent_entry.source == "agent"
    assert legacy_operator_entry.source == "operator"
