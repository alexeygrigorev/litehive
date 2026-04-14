from pathlib import Path

from litehive.config.engine_models import resolve_engine_name, resolve_engine_plan, resolve_model
from litehive.config.loading import load_config
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.domain.runtime import RuntimeEngineSwitch
from litehive.state.records import create_task


def test_resolve_engine_name_prefers_run_override_then_workspace_default(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Pending task")

    assert resolve_engine_name(task, config, engine_override="gemini") == "gemini"
    assert resolve_engine_name(task, config) == config.default_engine


def test_resolve_model_prefers_run_override_then_task_then_workspace_default(
    tmp_path: Path,
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="opencode",
            opencode_model="zai-coding-plan/glm-5.1",
        ),
    )
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Pending task", model="custom-task-model")

    assert (
        resolve_model(task, config, engine_name="opencode", model_override="run-model")
        == "run-model"
    )
    assert resolve_model(task, config, engine_name="opencode") == "custom-task-model"

    task.model = None
    assert resolve_model(task, config, engine_name="opencode") == "zai-coding-plan/glm-5.1"


def test_resolve_model_skips_unsupported_engine_override(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Pending task", model="custom-task-model")

    assert resolve_model(task, config, engine_name="codex", model_override="run-model") is None


def test_resolve_model_honors_goz_run_task_and_workspace_overrides(tmp_path: Path) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="goz",
            goz_model="glm-5-turbo",
        ),
    )
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Pending task", model="custom-task-model")

    assert resolve_model(task, config, engine_name="goz", model_override="run-model") == "run-model"
    assert resolve_model(task, config, engine_name="goz") == "custom-task-model"

    task.model = None
    assert resolve_model(task, config, engine_name="goz") == "glm-5-turbo"


def test_resolve_engine_name_ignores_title_keywords_uses_default(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior")

    assert resolve_engine_name(task, config) == "codex"
    assert resolve_engine_plan(task, config) == ["codex"]


def test_resolve_engine_name_ignores_task_type_for_engine_selection(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior", task_type="review")

    assert resolve_engine_name(task, config) == "codex"
    assert resolve_engine_plan(task, config) == ["codex"]


def test_resolve_engine_name_uses_default_engine_without_task_override(
    tmp_path: Path,
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="gemini",
        ),
    )
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior")

    assert resolve_engine_name(task, config) == "gemini"
    assert resolve_engine_plan(task, config) == ["gemini"]


def test_resolve_engine_name_honors_stage_matched_engine_switch(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Switch engine for retry")
    task.pipeline_status = "implementing"
    task.runtime.last_engine_switch = RuntimeEngineSwitch(
        step="implementing",
        from_engine="codex",
        to_engine="opencode",
        reason="codex recovery loop",
        happened_at="2026-04-14T17:00:00Z",
    )

    assert resolve_engine_name(task, config) == "opencode"
    assert resolve_engine_plan(task, config) == ["opencode"]
