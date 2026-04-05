import pytest

from .helpers import assert_successful_stage_result


pytestmark = pytest.mark.integration


def test_claude_emits_structured_stage_result(integration_root) -> None:
    assert_successful_stage_result("claude", cwd=integration_root)
