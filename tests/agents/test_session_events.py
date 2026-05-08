from pathlib import Path

from litehive.agents.session_events import (
    SubagentFinishedEvent,
    SubagentPidEvent,
    SubagentProgressEvent,
    SubagentStartedEvent,
)
from litehive.config.workspace import create_workspace
from litehive.domain.common import SubagentStatus
from litehive.observability.events import read_events
from litehive.state.records import create_task_for_workspace
from litehive.workspace import Workspace


def test_subagent_session_events_serialize_expected_kind_and_data() -> None:
    started = SubagentStartedEvent(
        subagent_id="SA-0001",
        role="swe",
        engine="codex",
        sandboxed=False,
    )
    pid = SubagentPidEvent(subagent_id="SA-0001", role="swe", pid=4242)
    progress = SubagentProgressEvent(subagent_id="SA-0001", role="swe", pid=4242)
    finished = SubagentFinishedEvent(
        subagent_id="SA-0001",
        role="swe",
        engine="codex",
        status=SubagentStatus.COMPLETED,
        exit_code=0,
        interruption_reason=None,
    )

    assert started.kind == "subagent_started"
    assert started.data() == {
        "subagent_id": "SA-0001",
        "role": "swe",
        "engine": "codex",
        "sandboxed": False,
    }
    assert pid.kind == "subagent_pid"
    assert pid.data() == {"subagent_id": "SA-0001", "role": "swe", "pid": 4242}
    assert progress.kind == "subagent_progress"
    assert progress.data() == {"subagent_id": "SA-0001", "role": "swe", "pid": 4242}
    assert finished.kind == "subagent_finished"
    assert finished.data() == {
        "subagent_id": "SA-0001",
        "role": "swe",
        "engine": "codex",
        "status": "completed",
        "exit_code": 0,
        "interruption_reason": None,
    }


def test_append_event_persists_typed_event_object(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Typed subagent event")
    event = SubagentPidEvent(subagent_id="SA-0001", role="swe", pid=4242)

    persisted = workspace.append_event(task, event)
    loaded = read_events(workspace, task)

    assert persisted["kind"] == "subagent_pid"
    assert persisted["data"] == {"subagent_id": "SA-0001", "role": "swe", "pid": 4242}
    assert loaded == [persisted]


def test_subagent_session_events_document_reason_and_consumers() -> None:
    event_classes = (
        SubagentStartedEvent,
        SubagentPidEvent,
        SubagentProgressEvent,
        SubagentFinishedEvent,
    )

    for event_class in event_classes:
        assert event_class.persistence_reason
        assert event_class.consumed_by
