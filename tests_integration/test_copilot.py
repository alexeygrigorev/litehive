import pytest

from .helpers import (
    assert_nudge_verdict_submission,
    assert_successful_smoke_session,
    prepare_smoke_session,
)


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def copilot_smoke_session(module_integration_root):
    return prepare_smoke_session("copilot", cwd=module_integration_root)


def test_copilot_smoke_prompt_succeeds(copilot_smoke_session) -> None:
    assert_successful_smoke_session(copilot_smoke_session)


def test_copilot_nudge_submits_verdict_via_cli(copilot_smoke_session) -> None:
    assert_nudge_verdict_submission("copilot", smoke_session=copilot_smoke_session)
