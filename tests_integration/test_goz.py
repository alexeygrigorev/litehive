import pytest

from .helpers import assert_nudge_verdict_submission, assert_successful_stage_result


pytestmark = pytest.mark.integration


def test_goz_emits_structured_stage_result(integration_root) -> None:
    assert_successful_stage_result("goz", cwd=integration_root)


def test_goz_nudge_submits_verdict_via_cli(integration_root) -> None:
    assert_nudge_verdict_submission("goz", cwd=integration_root)
