from litehive.config.model import LitehiveConfig
from litehive.lifecycle.engines import ConfigBackedEngineSelector


def test_config_backed_engine_selector_skips_frozen_engines() -> None:
    config = LitehiveConfig(
        engine_preference=["codex", "gemini"],
        engine_freeze={"codex": "2099-06-15T00:00:00Z"},
    )
    selector = ConfigBackedEngineSelector(config, lambda engine_name: engine_name)

    selected = selector.select(state=None, node_name="implementing", excluded=frozenset())  # type: ignore[arg-type]

    assert selected == "gemini"
