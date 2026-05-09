from datetime import datetime, timedelta, timezone
from pathlib import Path

from litehive.config.engine_models import (
    EngineSelectionRequest,
    EngineRoutingPolicy,
    resolve_engine_name,
    resolve_engine_plan,
    resolve_model,
    resolve_task_rejection_loop_limit,
)
from litehive.config.loading import WorkspaceConfigLoader
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import create_workspace
from litehive.domain.runtime import RuntimeEngineSwitch
from litehive.state.records import WorkspaceTasks
from litehive.domain.common import PipelineStatus
from litehive.workspace import Workspace


def _load_config(root: Path) -> LitehiveConfig:
    return WorkspaceConfigLoader(Workspace.from_path(root)).load()


def test_resolve_engine_name_prefers_run_override_then_workspace_default(
    tmp_path: Path,
) -> None:
    create_workspace(tmp_path)
    config = _load_config(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Pending task")

    assert resolve_engine_name(task, config, engine_override="gemini") == "gemini"
    assert resolve_engine_name(task, config) == config.default_engine


def test_select_engine_uses_explicit_request_candidates_after_exclusions(tmp_path: Path) -> None:
    create_workspace(tmp_path, LitehiveConfig(default_engine="codex", engine_preference=["codex", "gemini"]))
    workspace = Workspace.from_path(tmp_path)
    config = _load_config(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Explicit candidate routing")
    request = EngineSelectionRequest(
        engine_names=["gemini", "opencode", "codex"],
        excluded_engine_names={"gemini"},
        requested_model_name="run-model",
        check_quota=False,
    )

    selection = EngineRoutingPolicy(workspace, config).select(task, request)

    assert selection.engine_attempts == ["opencode", "codex"]
    assert selection.engine_name == "opencode"
    assert selection.model_name == "run-model"


def test_resolve_model_prefers_run_override_then_task_then_workspace_default(
    tmp_path: Path,
) -> None:
    create_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="opencode",
            opencode_model="zai-coding-plan/glm-5-turbo",
        ),
    )
    config = _load_config(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Pending task", model="custom-task-model")

    assert resolve_model(task, config, engine_name="opencode", requested_model_name="run-model") == "run-model"
    assert resolve_model(task, config, engine_name="opencode", requested_model_name=None) == "custom-task-model"

    task.model = None
    assert resolve_model(task, config, engine_name="opencode", requested_model_name=None) == "zai-coding-plan/glm-5-turbo"


def test_resolve_model_skips_unsupported_engine_override(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    config = _load_config(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Pending task", model="custom-task-model")

    assert resolve_model(task, config, engine_name="codex", requested_model_name="run-model") is None


def test_resolve_model_honors_goz_run_task_and_workspace_overrides(tmp_path: Path) -> None:
    create_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="goz",
            goz_model="glm-5-turbo",
        ),
    )
    config = _load_config(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Pending task", model="custom-task-model")

    assert resolve_model(task, config, engine_name="goz", requested_model_name="run-model") == "run-model"
    assert resolve_model(task, config, engine_name="goz", requested_model_name=None) == "custom-task-model"

    task.model = None
    assert resolve_model(task, config, engine_name="goz", requested_model_name=None) == "glm-5-turbo"


def test_resolve_engine_name_ignores_title_keywords_uses_default(
    tmp_path: Path,
) -> None:
    create_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = _load_config(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Research engine quota behavior")

    assert resolve_engine_name(task, config) == "codex"
    assert resolve_engine_plan(task, config) == ["codex"]


def test_config_builds_workspace_engine_attempt_order() -> None:
    config = LitehiveConfig(engine_preference=["gemini", "copilot"])

    assert config.engine_attempt_order(["codex"]) == ["codex", "gemini", "copilot"]


def test_resolve_engine_name_uses_first_unfrozen_attempt(tmp_path: Path) -> None:
    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    create_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            engine_preference=["gemini"],
            engine_freeze={"codex": future},
        ),
    )
    config = _load_config(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Frozen primary")

    assert resolve_engine_name(task, config) == "gemini"


def test_resolve_engine_name_uses_default_engine_without_task_override(
    tmp_path: Path,
) -> None:
    create_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="gemini",
        ),
    )
    config = _load_config(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Research engine quota behavior")

    assert resolve_engine_name(task, config) == "gemini"
    assert resolve_engine_plan(task, config) == ["gemini"]


def test_resolve_engine_name_honors_stage_matched_engine_switch(tmp_path: Path) -> None:
    create_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = _load_config(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Switch engine for retry")
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    task.runtime.execution.last_engine_switch = RuntimeEngineSwitch(
        stage="implementing",
        from_engine="codex",
        to_engine="opencode",
        reason="codex recovery loop",
        happened_at="2026-04-14T17:00:00Z",
    )

    assert resolve_engine_name(task, config) == "opencode"
    assert resolve_engine_plan(task, config) == ["opencode"]


def test_resolve_engine_name_run_override_beats_stage_matched_engine_switch(tmp_path: Path) -> None:
    create_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = _load_config(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Switch engine for retry")
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    task.runtime.execution.last_engine_switch = RuntimeEngineSwitch(
        stage="implementing",
        from_engine="codex",
        to_engine="opencode",
        reason="codex recovery loop",
        happened_at="2026-04-14T17:00:00Z",
    )

    assert resolve_engine_name(task, config, engine_override="gemini") == "gemini"
    assert resolve_engine_plan(task, config, engine_override="gemini") == ["gemini"]


def test_resolve_engine_name_ignores_stage_mismatched_engine_switch(tmp_path: Path) -> None:
    create_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = _load_config(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Switch engine for retry")
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    task.runtime.execution.last_engine_switch = RuntimeEngineSwitch(
        stage="testing",
        from_engine="codex",
        to_engine="opencode",
        reason="qa needed a different engine",
        happened_at="2026-04-14T17:00:00Z",
    )

    assert resolve_engine_name(task, config) == "codex"
    assert resolve_engine_plan(task, config) == ["codex"]


def test_resolve_task_rejection_loop_limit_uses_workspace_default(tmp_path: Path) -> None:
    create_workspace(tmp_path, LitehiveConfig(default_rejection_loop_limit=4))
    config = _load_config(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Workspace loop cap")

    assert resolve_task_rejection_loop_limit(task, config) == 4


def test_resolve_task_rejection_loop_limit_prefers_task_override(tmp_path: Path) -> None:
    create_workspace(tmp_path, LitehiveConfig(default_rejection_loop_limit=5))
    config = _load_config(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Task loop cap")
    task.retry_policy.rejection_loop_limit = 2

    assert resolve_task_rejection_loop_limit(task, config) == 2
