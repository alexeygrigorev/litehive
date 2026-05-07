from litehive.agents.session_store import (
    SubagentArtifactPayload,
    SubagentEventStreamPayload,
    load_subagent_event_stream,
    load_subagent_report,
    load_subagent_session,
    load_subagent_session_record,
    save_subagent_artifacts,
)
from litehive.config.workspace import ensure_workspace
from litehive.workspace import Workspace


def test_subagent_session_store_loads_named_slices_directly(tmp_path) -> None:
    ensure_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    save_subagent_artifacts(
        workspace,
        "T-0001",
        "SA-0001",
        session=SubagentArtifactPayload({"id": "SA-0001", "status": "running"}),
        report=SubagentArtifactPayload({"status": "running", "summary": "in progress"}),
        event_stream=SubagentEventStreamPayload({"events": [{"kind": "message"}]}),
    )

    assert load_subagent_session(workspace, "T-0001", "SA-0001") == {
        "id": "SA-0001",
        "status": "running",
    }
    assert load_subagent_report(workspace, "T-0001", "SA-0001") == {
        "status": "running",
        "summary": "in progress",
    }
    assert load_subagent_event_stream(workspace, "T-0001", "SA-0001") == {
        "events": [{"kind": "message"}],
    }


def test_subagent_session_store_event_stream_can_be_cleared(tmp_path) -> None:
    ensure_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    save_subagent_artifacts(
        workspace,
        "T-0001",
        "SA-0001",
        event_stream=SubagentEventStreamPayload({"events": [{"kind": "message"}]}),
    )
    save_subagent_artifacts(
        workspace,
        "T-0001",
        "SA-0001",
        clear_event_stream=True,
    )

    assert load_subagent_event_stream(workspace, "T-0001", "SA-0001") == {}


def test_subagent_session_record_normalizes_created_at(tmp_path) -> None:
    ensure_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    save_subagent_artifacts(
        workspace,
        "T-0001",
        "SA-0001",
        session=SubagentArtifactPayload({
            "id": " SA-0001 ",
            "role": " swe ",
            "created_at": "2026-05-07T10:00:00Z",
            "updated_at": "2026-05-07T10:01:00Z",
            "exit_code": 17,
        }),
    )

    session = load_subagent_session_record(workspace, "T-0001", "SA-0001")

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
    assert workspace.load_subagent_session_record("T-0001", "SA-0001") == session
    assert workspace.load_subagent_session_created_at("T-0001", "SA-0001") == "2026-05-07T10:00:00Z"


def test_subagent_session_record_falls_back_to_persisted_created_at(tmp_path) -> None:
    ensure_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    save_subagent_artifacts(
        workspace,
        "T-0001",
        "SA-0001",
        session=SubagentArtifactPayload({"id": "SA-0001"}),
    )

    session = load_subagent_session_record(workspace, "T-0001", "SA-0001")

    assert session.values == {"id": "SA-0001"}
    assert session.created_at is not None
    assert workspace.load_subagent_session_created_at("T-0001", "SA-0001") == session.created_at
