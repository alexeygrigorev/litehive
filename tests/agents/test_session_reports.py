from litehive.agents.sandbox import SandboxPolicySummary
from litehive.agents.session_reports import SubagentReportPayload
from litehive.domain.common import SubagentStatus


def test_subagent_report_payload_serializes_defensive_copies() -> None:
    files_changed = ["src/app.py"]
    tests = {"added": 1, "passing": 1}
    warnings = ["runner warning"]
    resource_control = SandboxPolicySummary(enabled=False)
    continuation = {"session_id": "session-123"}
    payload = SubagentReportPayload(
        status=SubagentStatus.COMPLETED,
        summary="done",
        files_changed=files_changed,
        tests=tests,
        warnings=warnings,
        resource_control=resource_control,
        interruption_reason="operator stopped",
        continuation=continuation,
    )

    serialized = payload.as_dict()
    files_changed.append("src/other.py")
    tests["passing"] = 2
    warnings.append("late warning")
    continuation["session_id"] = "changed"

    assert serialized == {
        "status": "completed",
        "summary": "done",
        "files_changed": ["src/app.py"],
        "tests": {"added": 1, "passing": 1},
        "warnings": ["runner warning"],
        "resource_control": {
            "enabled": False,
            "backend": None,
            "runtime": None,
            "image": None,
            "network_mode": None,
            "workspace_mode": None,
            "environment": [],
            "credential_inputs": [],
            "propagated_mounts": [],
        },
        "interruption_reason": "operator stopped",
        "continuation": {"session_id": "session-123"},
    }
