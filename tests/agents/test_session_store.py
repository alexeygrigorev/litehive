from litehive.agents.session_store import (
    SubagentArtifactPayload,
    SubagentEventStreamPayload,
    subagent_artifacts,
)
from litehive.config.workspace import create_workspace
from litehive.workspace import Workspace


def test_subagent_session_store_loads_named_slices_directly(tmp_path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    subagent_artifacts(workspace, "T-0001", "SA-0001").save(
        session=SubagentArtifactPayload({"id": "SA-0001", "status": "running"}),
        report=SubagentArtifactPayload({"status": "running", "summary": "in progress"}),
        event_stream=SubagentEventStreamPayload({"events": [{"kind": "message"}]}),
    )

    assert subagent_artifacts(workspace, "T-0001", "SA-0001").load_session_record().values == {
        "id": "SA-0001",
        "status": "running",
    }
    assert subagent_artifacts(workspace, "T-0001", "SA-0001").load_report() == {
        "status": "running",
        "summary": "in progress",
    }
    assert subagent_artifacts(workspace, "T-0001", "SA-0001").load_event_stream() == {
        "events": [{"kind": "message"}],
    }


def test_subagent_session_store_event_stream_can_be_cleared(tmp_path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    subagent_artifacts(workspace, "T-0001", "SA-0001").save(
        event_stream=SubagentEventStreamPayload({"events": [{"kind": "message"}]}),
    )
    subagent_artifacts(workspace, "T-0001", "SA-0001").save(
        clear_event_stream=True,
    )

    assert subagent_artifacts(workspace, "T-0001", "SA-0001").load_event_stream() == {}


def test_subagent_session_record_normalizes_created_at(tmp_path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    subagent_artifacts(workspace, "T-0001", "SA-0001").save(
        session=SubagentArtifactPayload({
            "id": " SA-0001 ",
            "role": " swe ",
            "created_at": "2026-05-07T10:00:00Z",
            "updated_at": "2026-05-07T10:01:00Z",
            "exit_code": 17,
        }),
    )

    session = subagent_artifacts(workspace, "T-0001", "SA-0001").load_session_record()

    assert session.values == {
        "id": " SA-0001 ",
        "role": " swe ",
        "created_at": "2026-05-07T10:00:00Z",
        "updated_at": "2026-05-07T10:01:00Z",
        "exit_code": 17,
    }
    assert session.subagent_id == "SA-0001"
    assert session.role == "swe"
    assert session.updated_at == "2026-05-07T10:01:00Z"
    assert session.exit_code == 17
    assert session.created_at == "2026-05-07T10:00:00Z"
    assert subagent_artifacts(workspace, "T-0001", "SA-0001").load_session_record() == session
    assert subagent_artifacts(workspace, "T-0001", "SA-0001").load_session_record() == session
    assert subagent_artifacts(workspace, "T-0001", "SA-0001").load_session_record().created_at == "2026-05-07T10:00:00Z"


def test_subagent_session_record_falls_back_to_persisted_created_at(tmp_path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    subagent_artifacts(workspace, "T-0001", "SA-0001").save(
        session=SubagentArtifactPayload({"id": "SA-0001"}),
    )

    session = subagent_artifacts(workspace, "T-0001", "SA-0001").load_session_record()

    assert session.values == {"id": "SA-0001"}
    assert session.created_at is not None
    assert subagent_artifacts(workspace, "T-0001", "SA-0001").load_session_record().created_at == session.created_at
