import pytest

from .helpers import (
    assert_nudge_verdict_submission,
    assert_successful_smoke_session,
    prepare_smoke_session,
)


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def claude_smoke_session(module_integration_root):
    return prepare_smoke_session("claude", cwd=module_integration_root)


def test_claude_smoke_prompt_succeeds(claude_smoke_session) -> None:
    assert_successful_smoke_session(claude_smoke_session)


def test_claude_nudge_submits_verdict_via_cli(claude_smoke_session) -> None:
    assert_nudge_verdict_submission("claude", smoke_session=claude_smoke_session)
