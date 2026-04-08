import pytest

from .helpers import assert_nudge_verdict_submission, assert_successful_stage_result


pytestmark = pytest.mark.integration


def test_opencode_emits_structured_stage_result(integration_root) -> None:
    assert_successful_stage_result("opencode", cwd=integration_root)


def test_opencode_nudge_submits_verdict_via_cli(integration_root) -> None:
    assert_nudge_verdict_submission("opencode", cwd=integration_root)
