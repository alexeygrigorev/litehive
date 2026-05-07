from litehive.agents.session_store import (
    load_subagent_event_stream,
    load_subagent_report,
    load_subagent_session,
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
        session={"id": "SA-0001", "status": "running"},
        report={"status": "running", "summary": "in progress"},
        event_stream={"events": [{"kind": "message"}]},
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


def test_subagent_session_store_slice_loaders_ignore_non_mapping_values(tmp_path) -> None:
    ensure_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    save_subagent_artifacts(
        workspace,
        "T-0001",
        "SA-0001",
        session=["not", "a", "mapping"],
        report="not a mapping",
        event_stream=[],
    )

    assert load_subagent_session(workspace, "T-0001", "SA-0001") == {}
    assert load_subagent_report(workspace, "T-0001", "SA-0001") == {}
    assert load_subagent_event_stream(workspace, "T-0001", "SA-0001") == {}
