from heru.types import RuntimeEngineContinuation

from litehive.agents.session_reports import SubagentReportPayload
from litehive.agents.session_snapshots import (
    RunningSubagentSessionRow,
    RunningSubagentSessionMetadata,
    SubagentSessionMetadata,
    SubagentSessionSnapshot,
    SubagentSessionStorageFields,
    TerminalSubagentSessionRow,
)
from litehive.domain.common import SubagentStatus


def test_subagent_session_metadata_serializes_continuation() -> None:
    metadata = SubagentSessionMetadata(
        exit_code=0,
        pid=4242,
        interruption_reason=None,
        continuation=RuntimeEngineContinuation(session_id="session-123"),
    )

    payload = metadata.continuation_payload()

    assert payload is not None
    assert payload["session_id"] == "session-123"
    assert "updated_at" in payload


def test_running_subagent_session_metadata_only_carries_running_fields() -> None:
    metadata = RunningSubagentSessionMetadata(
        pid=4242,
        continuation=RuntimeEngineContinuation(session_id="session-123"),
    )

    payload = metadata.continuation_payload()

    assert metadata.pid == 4242
    assert payload is not None
    assert payload["session_id"] == "session-123"


def test_running_subagent_session_row_serializes_without_terminal_fields() -> None:
    fields = SubagentSessionStorageFields(
        id="SA-0001",
        role="swe",
        engine="codex",
        status=SubagentStatus.RUNNING,
        sandboxed=True,
        sandbox="workspace-write",
        created_at="created",
        updated_at="updated",
        resource_control={"policy": "sandboxed"},
    )
    row = RunningSubagentSessionRow(fields=fields, pid=None, continuation=None)

    payload = row.as_dict()

    assert payload["status"] == "running"
    assert payload["pid"] is None
    assert payload["exit_code"] is None
    assert payload["interruption_reason"] is None


def test_terminal_subagent_session_row_requires_exit_code() -> None:
    fields = SubagentSessionStorageFields(
        id="SA-0001",
        role="swe",
        engine="codex",
        status=SubagentStatus.COMPLETED,
        sandboxed=True,
        sandbox="workspace-write",
        created_at="created",
        updated_at="updated",
        resource_control={"policy": "sandboxed"},
    )
    row = TerminalSubagentSessionRow(fields=fields, exit_code=0, pid=4242)

    payload = row.as_dict()

    assert payload["status"] == "completed"
    assert payload["pid"] == 4242
    assert payload["exit_code"] == 0
    assert payload["interruption_reason"] is None


def test_subagent_session_snapshot_groups_streams_report_and_metadata() -> None:
    report = SubagentReportPayload(status=SubagentStatus.RUNNING, summary="")
    metadata = SubagentSessionMetadata(exit_code=None, pid=None)

    snapshot = SubagentSessionSnapshot(
        prompt="prompt",
        transcript="trace",
        stdout="out",
        stderr="err",
        report=report,
        metadata=metadata,
    )

    assert snapshot.report is report
    assert snapshot.metadata is metadata
    assert snapshot.stdout == "out"
    assert snapshot.stderr == "err"
