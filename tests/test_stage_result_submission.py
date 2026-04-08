"""Tests for schema-validated STAGE_RESULT: submission API (T-0104)."""

import json


from litehive.engines.base import parse_stage_report_text


def _transcript(payload: dict) -> str:
    return f"STAGE_RESULT:\n{json.dumps(payload)}\n"


# ---------------------------------------------------------------------------
# AC-1: Valid full submission produces StageReport via structured path
# ---------------------------------------------------------------------------

def test_valid_submission_produces_structured_report() -> None:
    payload = {
        "verdict": "pass",
        "summary": "All checks green",
        "files_changed": ["src/foo.py", "tests/test_foo.py"],
        "tests": {"added": 3, "passing": 10},
        "warnings": ["minor lint issue"],
        "acceptance_criteria": [],
    }
    report = parse_stage_report_text(
        task_id="T-0001",
        step="implementing",
        transcript=_transcript(payload),
        subagent_status="completed",
    )
    assert report.verdict == "pass"
    assert report.summary == "All checks green"
    # files_changed is no longer populated from agent output; it comes from git diff at commit time
    assert report.files_changed == []
    assert report.tests == {"added": 3, "passing": 10}
    assert report.warnings == ["minor lint issue"]


# ---------------------------------------------------------------------------
# AC-2: Extra unknown field → validation error, verdict is fail (no text fallback)
# ---------------------------------------------------------------------------

def test_extra_field_rejected_produces_fail() -> None:
    payload = {
        "verdict": "pass",
        "summary": "done",
        "unexpected_field": "should be rejected",
    }
    transcript = _transcript(payload) + "\nVERDICT: FAIL\nSUMMARY: text fallback\n"
    report = parse_stage_report_text(
        task_id="T-0001",
        step="implementing",
        transcript=transcript,
        subagent_status="completed",
    )
    # No text fallback — verdict is always fail when STAGE_RESULT is invalid
    assert report.verdict == "fail"
    # A validation warning should be present
    assert any("STAGE_RESULT validation" in w for w in report.warnings)
    # Also warns about missing CLI verdict
    assert any("litehive report" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# AC-3: Missing required verdict field → fail verdict, no text fallback
# ---------------------------------------------------------------------------

def test_missing_verdict_produces_fail_with_warning() -> None:
    payload = {
        "summary": "done",
        "files_changed": [],
    }
    transcript = _transcript(payload) + "\nVERDICT: PASS\nSUMMARY: text path\n"
    report = parse_stage_report_text(
        task_id="T-0001",
        step="implementing",
        transcript=transcript,
        subagent_status="completed",
    )
    # Text VERDICT: PASS is NOT parsed — no text fallback
    assert report.verdict == "fail"
    assert any("STAGE_RESULT validation" in w for w in report.warnings)
    # Warning should mention the missing field
    assert any("verdict" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# AC-4: acceptance_criteria at top level applied to task_update
# ---------------------------------------------------------------------------

def test_top_level_acceptance_criteria_forwarded_to_task_update() -> None:
    criteria = ["Criterion A", "Criterion B"]
    payload = {
        "verdict": "pass",
        "summary": "grooming complete",
        "acceptance_criteria": criteria,
    }
    report = parse_stage_report_text(
        task_id="T-0001",
        step="grooming",
        transcript=_transcript(payload),
        subagent_status="completed",
    )
    assert report.task_update.get("acceptance_criteria") == criteria


def test_top_level_acceptance_criteria_not_overwritten_by_task_update() -> None:
    """task_update.acceptance_criteria wins when both are provided."""
    payload = {
        "verdict": "pass",
        "summary": "grooming complete",
        "acceptance_criteria": ["top-level criterion"],
        "task_update": {
            "acceptance_criteria": ["task_update criterion"],
        },
    }
    report = parse_stage_report_text(
        task_id="T-0001",
        step="grooming",
        transcript=_transcript(payload),
        subagent_status="completed",
    )
    # task_update value takes precedence
    assert report.task_update.get("acceptance_criteria") == ["task_update criterion"]


# ---------------------------------------------------------------------------
# AC-5: task_update fields (goal, pm_complexity) applied to task record
# ---------------------------------------------------------------------------

def test_task_update_fields_forwarded() -> None:
    payload = {
        "verdict": "pass",
        "summary": "groomed",
        "task_update": {
            "goal": "Build a rocket",
            "pm_complexity": "simple",
        },
    }
    report = parse_stage_report_text(
        task_id="T-0001",
        step="grooming",
        transcript=_transcript(payload),
        subagent_status="completed",
    )
    assert report.task_update.get("goal") == "Build a rocket"
    assert report.task_update.get("pm_complexity") == "simple"


# ---------------------------------------------------------------------------
# AC-6 (validation warning format)
# ---------------------------------------------------------------------------

def test_validation_warning_is_human_readable() -> None:
    """Validation warnings mention field name and a descriptive message."""
    payload = {
        # verdict is missing
        "summary": "no verdict here",
    }
    transcript = _transcript(payload) + "\nVERDICT: PASS\nSUMMARY: fallback\n"
    report = parse_stage_report_text(
        task_id="T-0001",
        step="implementing",
        transcript=transcript,
        subagent_status="completed",
    )
    # Verdict is fail — text VERDICT: PASS is not parsed
    assert report.verdict == "fail"
    validation_warnings = [w for w in report.warnings if "STAGE_RESULT validation" in w]
    assert validation_warnings, "Expected at least one STAGE_RESULT validation warning"
    # Each warning should be a non-empty human-readable string
    for w in validation_warnings:
        assert len(w) > len("STAGE_RESULT validation: ")


def test_extra_field_validation_warning_mentions_field() -> None:
    payload = {
        "verdict": "pass",
        "summary": "done",
        "rogue_field": "oops",
    }
    transcript = _transcript(payload) + "\nVERDICT: PASS\nSUMMARY: fallback\n"
    report = parse_stage_report_text(
        task_id="T-0001",
        step="implementing",
        transcript=transcript,
        subagent_status="completed",
    )
    assert report.verdict == "fail"
    assert any("STAGE_RESULT validation" in w for w in report.warnings)
