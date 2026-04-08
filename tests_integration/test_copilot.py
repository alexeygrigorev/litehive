import pytest

from .helpers import assert_nudge_verdict_submission, assert_successful_stage_result


pytestmark = pytest.mark.integration


def test_copilot_emits_structured_stage_result(integration_root) -> None:
    assert_successful_stage_result("copilot", cwd=integration_root)


def test_copilot_nudge_submits_verdict_via_cli(integration_root) -> None:
    assert_nudge_verdict_submission("copilot", cwd=integration_root)
