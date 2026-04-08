import pytest

from .helpers import assert_nudge_verdict_submission, assert_successful_stage_result


pytestmark = pytest.mark.integration


def test_gemini_emits_structured_stage_result(integration_root) -> None:
    assert_successful_stage_result("gemini", cwd=integration_root)


def test_gemini_nudge_submits_verdict_via_cli(integration_root) -> None:
    assert_nudge_verdict_submission("gemini", cwd=integration_root)
