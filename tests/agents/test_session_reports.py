from litehive.agents.session_reports import SubagentReportPayload


def test_subagent_report_payload_serializes_defensive_copies() -> None:
    files_changed = ["src/app.py"]
    tests = {"added": 1, "passing": 1}
    warnings = ["runner warning"]
    resource_control = {"enabled": False}
    continuation = {"session_id": "session-123"}
    payload = SubagentReportPayload(
        status="completed",
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
    resource_control["enabled"] = True
    continuation["session_id"] = "changed"

    assert serialized == {
        "status": "completed",
        "summary": "done",
        "files_changed": ["src/app.py"],
        "tests": {"added": 1, "passing": 1},
        "warnings": ["runner warning"],
        "resource_control": {"enabled": False},
        "interruption_reason": "operator stopped",
        "continuation": {"session_id": "session-123"},
    }
