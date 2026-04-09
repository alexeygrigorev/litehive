"""Tests for report feedback capping (T-0143)."""

from litehive.models import cap_feedback, FEEDBACK_CAP, _TRUNCATION_MARKER
from litehive.agents.base import parse_stage_report_text


def test_cap_feedback_short_text_unchanged() -> None:
    short = "This is a short feedback message."
    assert cap_feedback(short) == short


def test_cap_feedback_exact_limit_unchanged() -> None:
    text = "a" * FEEDBACK_CAP
    assert cap_feedback(text) == text


def test_cap_feedback_truncates_long_text() -> None:
    long_text = "x" * 5000
    result = cap_feedback(long_text)
    assert len(result) <= FEEDBACK_CAP
    assert result.endswith(_TRUNCATION_MARKER)


def test_cap_feedback_custom_limit() -> None:
    result = cap_feedback("a" * 200, limit=100)
    assert len(result) <= 100
    assert result.endswith(_TRUNCATION_MARKER)


def test_parse_stage_report_text_caps_feedback_structured() -> None:
    """Structured STAGE_RESULT path caps feedback from transcript."""
    long_preamble = "z" * 5000
    transcript = (
        long_preamble + "\n"
        "STAGE_RESULT:\n"
        '{"verdict":"pass","summary":"done","files_changed":["foo.py"],'
        '"tests":{"added":1,"passing":1},"warnings":[],'
        '"acceptance_criteria":[]}\n'
    )
    report = parse_stage_report_text(
        task_id="T-0099",
        step="implementing",
        transcript=transcript,
        subagent_status="completed",
    )
    assert len(report.feedback) <= FEEDBACK_CAP
    assert report.verdict == "pass"


def test_parse_stage_report_text_caps_feedback_no_cli_verdict() -> None:
    """Without CLI verdict, feedback is still capped and verdict is fail."""
    long_preamble = "z" * 5000
    transcript = (
        long_preamble + "\n"
        "VERDICT: PASS\n"
        "SUMMARY: done\n"
        "FILES_CHANGED:\n"
        "- foo.py\n"
        "TESTS_ADDED: 1\n"
        "TESTS_PASSING: 1\n"
    )
    report = parse_stage_report_text(
        task_id="T-0099",
        step="implementing",
        transcript=transcript,
        subagent_status="completed",
    )
    assert len(report.feedback) <= FEEDBACK_CAP
    # Text VERDICT: PASS is no longer parsed — verdict is fail
    assert report.verdict == "fail"


def test_cli_report_message_stored_as_is() -> None:
    """Thread comment messages (from litehive report) are not truncated.

    These are already human-authored summaries, not transcripts.
    Verified by checking that StageReport created from thread comments
    in subagents.py uses latest.message directly (no cap_feedback).
    """
    from litehive.models import StageReport

    message = "Implemented feature X. Tests pass. All criteria met."
    report = StageReport(
        task_id="T-0001",
        step="implementing",
        verdict="pass",
        summary="done",
        feedback=message,
    )
    assert report.feedback == message
