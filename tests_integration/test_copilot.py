import pytest

from .helpers import assert_successful_stage_result


pytestmark = pytest.mark.integration


def test_copilot_emits_structured_stage_result(integration_root) -> None:
    assert_successful_stage_result("copilot", cwd=integration_root)
