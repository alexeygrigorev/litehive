from litehive.agents import engine_manager
from litehive.agents.engine_manager import EngineManager


def test_engine_manager_resolves_engines_through_single_collaborator(monkeypatch) -> None:
    engine = object()
    calls: list[str] = []

    def fake_get_engine(engine_name: str):
        calls.append(engine_name)
        return engine

    monkeypatch.setattr(engine_manager, "get_engine", fake_get_engine)

    assert EngineManager().engine_for("codex") is engine
    assert calls == ["codex"]


def test_engine_manager_resolves_resume_safe_model(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_resume_safe_model_override(engine_name, model, resume_session_id=None):
        captured["engine_name"] = engine_name
        captured["model"] = model
        captured["resume_session_id"] = resume_session_id
        return "safe-model"

    monkeypatch.setattr(engine_manager, "resume_safe_model_override", fake_resume_safe_model_override)

    assert EngineManager().resume_safe_model("codex", "gpt-5.2", "session-1") == "safe-model"
    assert captured == {
        "engine_name": "codex",
        "model": "gpt-5.2",
        "resume_session_id": "session-1",
    }
