import pytest

from .helpers import (
    assert_nudge_verdict_submission,
    assert_successful_smoke_session,
    prepare_smoke_session,
)


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def opencode_smoke_session(module_integration_root):
    return prepare_smoke_session("opencode", cwd=module_integration_root)


def test_opencode_smoke_prompt_succeeds(opencode_smoke_session) -> None:
    assert_successful_smoke_session(opencode_smoke_session)


def test_opencode_nudge_submits_verdict_via_cli(opencode_smoke_session) -> None:
    assert_nudge_verdict_submission("opencode", smoke_session=opencode_smoke_session)
