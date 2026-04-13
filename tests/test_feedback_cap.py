"""Tests for report feedback capping (T-0143)."""

from litehive.models.common import cap_feedback, FEEDBACK_CAP, TRUNCATION_MARKER


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
    assert result.endswith(TRUNCATION_MARKER)


def test_cap_feedback_custom_limit() -> None:
    result = cap_feedback("a" * 200, limit=100)
    assert len(result) <= 100
    assert result.endswith(TRUNCATION_MARKER)
