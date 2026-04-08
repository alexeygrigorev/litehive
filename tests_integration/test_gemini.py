import pytest

from .helpers import (
    assert_nudge_verdict_submission,
    assert_successful_smoke_session,
    prepare_smoke_session,
)


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def gemini_smoke_session(module_integration_root):
    return prepare_smoke_session("gemini", cwd=module_integration_root)


def test_gemini_smoke_prompt_succeeds(gemini_smoke_session) -> None:
    assert_successful_smoke_session(gemini_smoke_session)


def test_gemini_nudge_submits_verdict_via_cli(gemini_smoke_session) -> None:
    assert_nudge_verdict_submission("gemini", smoke_session=gemini_smoke_session)
