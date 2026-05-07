from heru.types import RuntimeEngineContinuation

from litehive.sandbox.launcher import SandboxPolicySummary
from litehive.agents.session_continuation import CapturedSubagentContinuation, NoSubagentContinuation
from litehive.agents.session_reports import SubagentReportPayload
from litehive.agents.session_snapshots import (
    InterruptedSubagentSessionRow,
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
        continuation=CapturedSubagentContinuation(RuntimeEngineContinuation(session_id="session-123")),
    )

    payload = metadata.continuation_payload()

    assert payload is not None
    assert payload["session_id"] == "session-123"
    assert "updated_at" in payload


def test_running_subagent_session_metadata_only_carries_running_fields() -> None:
    metadata = RunningSubagentSessionMetadata(
        pid=4242,
        continuation=CapturedSubagentContinuation(RuntimeEngineContinuation(session_id="session-123")),
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
        resource_control=SandboxPolicySummary(enabled=True, backend="docker"),
    )
    row = RunningSubagentSessionRow(fields=fields, pid=None, continuation=NoSubagentContinuation())

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
        resource_control=SandboxPolicySummary(enabled=True, backend="docker"),
    )
    row = TerminalSubagentSessionRow(fields=fields, exit_code=0, pid=4242)

    payload = row.as_dict()

    assert payload["status"] == "completed"
    assert payload["pid"] == 4242
    assert payload["exit_code"] == 0
    assert payload["interruption_reason"] is None


def test_interrupted_subagent_session_row_keeps_resume_fields_separate() -> None:
    fields = SubagentSessionStorageFields(
        id="SA-0001",
        role="swe",
        engine="codex",
        status=SubagentStatus.INTERRUPTED,
        sandboxed=True,
        sandbox="workspace-write",
        created_at="created",
        updated_at="updated",
        resource_control=SandboxPolicySummary(enabled=True, backend="docker"),
    )
    row = InterruptedSubagentSessionRow(
        fields=fields,
        pid=4242,
        exit_code=None,
        interruption_reason="received ctrl-c",
        resume_stage="implementing",
        continuation=NoSubagentContinuation(),
    )

    payload = row.as_dict()

    assert payload["status"] == "interrupted"
    assert payload["pid"] == 4242
    assert payload["exit_code"] is None
    assert payload["interruption_reason"] == "received ctrl-c"
    assert payload["resume_stage"] == "implementing"


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
