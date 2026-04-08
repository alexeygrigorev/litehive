import pytest

from .helpers import (
    assert_nudge_verdict_submission,
    assert_successful_smoke_session,
    prepare_smoke_session,
)


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def goz_smoke_session(module_integration_root):
    return prepare_smoke_session("goz", cwd=module_integration_root)


def test_goz_smoke_prompt_succeeds(goz_smoke_session) -> None:
    assert_successful_smoke_session(goz_smoke_session)


def test_goz_nudge_submits_verdict_via_cli(goz_smoke_session) -> None:
    assert_nudge_verdict_submission("goz", smoke_session=goz_smoke_session)
