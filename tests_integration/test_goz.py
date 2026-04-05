import pytest

from .helpers import assert_successful_stage_result


pytestmark = pytest.mark.integration


def test_goz_emits_structured_stage_result(integration_root) -> None:
    assert_successful_stage_result("goz", cwd=integration_root)
