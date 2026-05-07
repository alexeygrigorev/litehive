from heru.types import RuntimeEngineContinuation

from litehive.agents.session_reports import SubagentReportPayload
from litehive.agents.session_snapshots import SubagentSessionMetadata, SubagentSessionSnapshot


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


def test_subagent_session_snapshot_groups_streams_report_and_metadata() -> None:
    report = SubagentReportPayload(status="running", summary="")
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
