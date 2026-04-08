import pytest

from .helpers import assert_nudge_verdict_submission, assert_successful_stage_result


pytestmark = pytest.mark.integration


def test_claude_emits_structured_stage_result(integration_root) -> None:
    assert_successful_stage_result("claude", cwd=integration_root)


def test_claude_nudge_submits_verdict_via_cli(integration_root) -> None:
    assert_nudge_verdict_submission("claude", cwd=integration_root)
