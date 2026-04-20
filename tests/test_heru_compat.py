from heru.base import LATEST_CONTINUATION_SENTINEL
from heru.types import RuntimeEngineContinuation

from litehive.heru_compat import resolve_engine_resume_session_id, resume_safe_model_override


def test_resolve_engine_resume_session_id_prefers_continuation_resume_id() -> None:
    continuation = RuntimeEngineContinuation(session_id="resume-123")

    assert resolve_engine_resume_session_id("codex", continuation) == "resume-123"


def test_resolve_engine_resume_session_id_prefers_concrete_resume_id_even_when_latest_is_allowed() -> None:
    continuation = RuntimeEngineContinuation(session_id="resume-123")

    assert resolve_engine_resume_session_id("codex", continuation, prefer_latest=True) == "resume-123"


def test_resolve_engine_resume_session_id_can_fall_back_to_continue_latest() -> None:
    continuation = RuntimeEngineContinuation()

    assert resolve_engine_resume_session_id("claude", continuation, prefer_latest=True) == LATEST_CONTINUATION_SENTINEL
    assert resolve_engine_resume_session_id("goz", continuation, prefer_latest=True) is None


def test_resume_safe_model_override_drops_opencode_model_for_resume() -> None:
    assert resume_safe_model_override("opencode", "zai-coding-plan/glm-5.1", resume_session_id="session-123") is None
    assert resume_safe_model_override("codex", "gpt-5.4", resume_session_id="session-123") == "gpt-5.4"
