from types import SimpleNamespace

from litehive.config.engine_models import EngineSelection
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.lifecycle.engines import ConfigBackedEngineSelector
from litehive.state.records import create_task


def test_config_backed_engine_selector_skips_frozen_engines() -> None:
    config = LitehiveConfig(
        engine_preference=["codex", "gemini"],
        engine_freeze={"codex": "2099-06-15T00:00:00Z"},
    )
    selector = ConfigBackedEngineSelector(config, lambda engine_name: engine_name)

    selected = selector.select(state=None, node_name="implementing", excluded=frozenset())  # type: ignore[arg-type]

    assert selected == "gemini"


def test_config_backed_engine_selector_uses_shared_task_selection(
    tmp_path,
    monkeypatch,
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            engine_preference=["codex", "goz"],
        ),
    )
    task = create_task(tmp_path, title="Selector preview")

    class FakeEngine:
        def __init__(self, name: str, *, model_name: str | None = None) -> None:
            self.name = name
            self.model_name = model_name

        def with_model(self, model_name: str | None):
            return FakeEngine(self.name, model_name=model_name)

    monkeypatch.setattr(
        "litehive.lifecycle.engines.select_engine",
        lambda *args, **kwargs: EngineSelection(
            engine_name="goz",
            model_name="goz-live-model",
            engine_attempts=["codex", "goz"],
            skipped=[],
        ),
    )

    selector = ConfigBackedEngineSelector(
        LitehiveConfig(default_engine="codex", engine_preference=["codex", "goz"]),
        lambda engine_name: FakeEngine(engine_name),
        workspace_root=tmp_path,
        model_override="ignored-by-mock",
    )

    selected = selector.select(
        state=SimpleNamespace(task_id=task.id),
        node_name="implementing",
        excluded=frozenset({"codex"}),
    )

    assert selected is not None
    assert selected.name == "goz"
    assert selected.model_name == "goz-live-model"
