from tests.workspace_helpers import (
    LitehiveConfig,
    Path,
    _cmd_update,
    argparse,
    create_task,
    ensure_workspace,
    global_config_path,
    get_task,
    load_config,
    pytest,
    resolve_engine_name,
    resolve_engine_plan,
    resolve_model,
    yaml,
)

from litehive.agents import ENGINE_CHOICES
from typer.testing import CliRunner

from litehive.cli import app


def test_engine_command_freezes_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    result = CliRunner().invoke(
        app,
        ["engine", "freeze", "gemini", "--workspace", str(tmp_path), "--until", "2099-01-02"],
        standalone_mode=False,
    )
    output = result.output

    assert result.return_value == 0
    config = load_config(tmp_path)
    assert config.default_engine == "codex"
    raw_config = yaml.safe_load(
        (tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8")
    )
    assert raw_config["engine_freeze"]["gemini"] == "2099-01-02T00:00:00Z"
    assert "engine_frozen: gemini until 2099-01-02T00:00:00Z" in output



def test_resolve_workspace_uses_workspace_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)

    from litehive.config import resolve_workspace

    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("LITEHIVE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)

    assert resolve_workspace(None) == tmp_path.resolve()


def test_resolve_workspace_walks_up_and_normalizes_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Walk up worktree")
    from litehive.config import worktree_root

    nested = worktree_root(tmp_path) / task.id / "src"
    nested.mkdir(parents=True)

    from litehive.config import resolve_workspace

    monkeypatch.chdir(nested)
    monkeypatch.setenv("LITEHIVE_TASK_ID", task.id)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)

    assert resolve_workspace(None) == tmp_path.resolve()


def test_resolve_workspace_prefers_current_unified_root_worktree_over_registry_task_id_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITEHIVE_HOME", str(tmp_path / "litehive-home"))

    workspace_one = tmp_path / "workspace-one"
    workspace_two = tmp_path / "workspace-two"
    ensure_workspace(workspace_one)
    ensure_workspace(workspace_two)
    task_one = create_task(workspace_one, title="first task")
    task_two = create_task(workspace_two, title="second task")

    assert task_one.id == task_two.id == "T-0001"

    from litehive.config import resolve_workspace, worktree_root

    nested = worktree_root(workspace_two) / task_two.id / "src"
    nested.mkdir(parents=True)

    monkeypatch.chdir(nested)
    monkeypatch.setenv("LITEHIVE_TASK_ID", task_two.id)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)

    assert resolve_workspace(None) == workspace_two.resolve()


def test_resolve_workspace_prefers_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    from litehive.config import resolve_workspace

    monkeypatch.chdir(outside)
    monkeypatch.setenv("LITEHIVE_WORKSPACE_ROOT", str(outside))

    assert resolve_workspace(None, workspace=tmp_path) == tmp_path.resolve()


def test_resolve_workspace_uses_registry_from_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Registry lookup")
    outside = tmp_path / "outside"
    outside.mkdir()

    from litehive.config import resolve_workspace

    monkeypatch.chdir(outside)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)

    assert resolve_workspace(task.id) == tmp_path.resolve()


def test_resolve_workspace_rejects_unresolved_workspace_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)

    from litehive.config import resolve_workspace

    monkeypatch.setenv("LITEHIVE_WORKSPACE_ROOT", "$tmpdir/project")
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)

    with pytest.raises(ValueError, match="unresolved shell variable"):
        resolve_workspace(None)


def test_ensure_workspace_rejects_nested_workspace_root(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    nested_root = tmp_path / ".litehive" / "worktrees" / "T-0001"
    nested_root.mkdir(parents=True)

    with pytest.raises(ValueError, match="managed worktrees.*choose the real repo root"):
        ensure_workspace(nested_root)


def test_ensure_workspace_rejects_litehive_control_directory(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    with pytest.raises(ValueError, match="Litehive control directory.*choose the real repo root"):
        ensure_workspace(tmp_path / ".litehive")


def test_ensure_workspace_rejects_nested_litehive_control_directory(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    with pytest.raises(ValueError, match="Litehive control directory.*choose the real repo root"):
        ensure_workspace(tmp_path / ".litehive" / ".litehive")


def test_ensure_workspace_rejects_nested_subdirectory_of_existing_workspace(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    nested_root = tmp_path / "packages" / "demo"
    nested_root.mkdir(parents=True)

    with pytest.raises(ValueError, match="inside existing Litehive workspace.*nested subdirectory"):
        ensure_workspace(nested_root)


def test_ensure_workspace_rejects_leading_unresolved_shell_var() -> None:
    with pytest.raises(ValueError, match="unresolved shell variable syntax.*expanded absolute path"):
        ensure_workspace(Path("$tmpdir/project"))


def test_ensure_workspace_rejects_embedded_unresolved_shell_var() -> None:
    with pytest.raises(ValueError, match="unresolved shell variable syntax.*expanded absolute path"):
        ensure_workspace(Path("/tmp/$tmpdir/project"))


def test_ensure_workspace_rejects_braced_unresolved_shell_var() -> None:
    with pytest.raises(ValueError, match="unresolved shell variable syntax.*expanded absolute path"):
        ensure_workspace(Path("/tmp/${tmpdir}/project"))


def test_engine_status_command_shows_compact_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            engine_freeze={"gemini": "2099-06-15T00:00:00Z"},
        ),
    )

    result = CliRunner().invoke(
        app,
        ["engine", "status", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )
    output = result.output.strip()

    assert result.return_value == 0
    assert output.startswith("default_engine: codex | engine_freeze: gemini=2099-06-15T00:00:00Z | engines: ")
    for engine_name in ENGINE_CHOICES:
        assert f"{engine_name}(available=" in output


def test_switch_command_parser_accepts_task_engine_reason_and_workspace() -> None:
    assert True


def test_configure_persists_gemini_model(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["default_engine"] = "gemini"
    raw_config["gemini_model"] = "gemini-2.5-pro"
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.default_engine == "gemini"
    assert config.gemini_model == "gemini-2.5-pro"


def test_configure_persists_copilot_model(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["default_engine"] = "copilot"
    raw_config["copilot_model"] = "gpt-5"
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.default_engine == "copilot"
    assert config.copilot_model == "gpt-5"


def test_configure_persists_process_profile(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load((tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8"))
    raw_config["process_profile"] = "rust"
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.process_profile == "rust"


def test_load_config_uses_global_defaults_when_workspace_config_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    global_path = global_config_path()
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        yaml.safe_dump(
            {
                "default_engine": "gemini",
                "pool_stop_on_failure": True,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    (workspace / ".litehive" / "config.yaml").write_text("{}", encoding="utf-8")

    config = load_config(workspace)

    assert config.default_engine == "gemini"
    assert config.pool_stop_on_failure is True


def test_load_config_applies_workspace_overrides_on_top_of_global_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    global_path = global_config_path()
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        yaml.safe_dump(
            {
                "default_engine": "gemini",
                "pool_max_tasks": 7,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    (workspace / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "default_engine": "codex",
                "pool_max_tasks": 2,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(workspace)

    assert config.default_engine == "codex"
    assert config.pool_max_tasks == 2


def test_load_config_deep_merges_global_and_workspace_mappings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    global_path = global_config_path()
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        yaml.safe_dump(
            {
                "engine_freeze": {"gemini": "2099-01-01T00:00:00Z"},
                "agent_startup_guidance": {"swe": ["global guidance"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    (workspace / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "engine_freeze": {"codex": "2099-02-02T00:00:00Z"},
                "agent_startup_guidance": {"swe": ["project guidance"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(workspace)

    assert config.engine_freeze == {
        "gemini": "2099-01-01T00:00:00Z",
        "codex": "2099-02-02T00:00:00Z",
    }
    assert config.agent_startup_guidance == {"swe": ["project guidance"]}


def test_resolve_engine_name_prefers_run_override_then_workspace_default(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Pending task")

    assert resolve_engine_name(task, config, engine_override="gemini") == "gemini"
    assert resolve_engine_name(task, config) == config.default_engine


def test_create_task_rejects_removed_engine_override(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    with pytest.raises(TypeError):
        create_task(tmp_path, title="Pending task", engine="gemini")

    assert load_config(tmp_path).default_engine == "codex"


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


def test_litehive_config_normalizes_retry_on() -> None:
    config = LitehiveConfig(retry_on=["timeout", "network", "timeout", "execution_limit"])

    assert config.retry_on == ["timeout", "network", "execution_limit"]


def test_litehive_config_defaults_include_flat_retry_on() -> None:
    config = LitehiveConfig()

    assert config.subagent_inactivity_timeout_seconds == 360.0
    assert config.retry_on == ["execution_limit", "timeout"]


def test_load_config_reads_subagent_inactivity_timeout_override(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load(
        (tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8")
    )
    raw_config["subagent_inactivity_timeout_seconds"] = 42
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.subagent_inactivity_timeout_seconds == 42.0


def test_litehive_config_rejects_unknown_retry_on_kind() -> None:
    with pytest.raises(ValueError, match="retry_on must contain only"):
        LitehiveConfig(retry_on=["timeout", "rate_limit"])


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


def test_configure_no_longer_has_task_engine_routing(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_text = (tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8")
    assert "task_engine_route" not in raw_text
    config = load_config(tmp_path)
    assert config.default_engine == "codex"


def test_load_config_rejects_legacy_pre_acceptance_command(tmp_path: Path) -> None:
    from litehive.config import ensure_workspace, config_path

    ensure_workspace(tmp_path)
    import yaml

    cfg = yaml.safe_load(config_path(tmp_path).read_text(encoding="utf-8")) or {}
    cfg["pre_acceptance_command"] = "uv run ruff check litehive tests"
    config_path(tmp_path).write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="pre_acceptance_command is no longer supported"):
        load_config(tmp_path)


def test_configure_persists_runner_hooks(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner_hooks": {
                    "before_implementing": [{"command": "echo pre"}],
                    "after_implementing": [{"command": "echo post", "reject_on_failure": True}],
                    "before_accepting": [{"command": "echo review"}],
                    "after_accepting": [{"command": "echo accepted"}],
                    "after_commit": [{"command": "echo verify", "reject_on_failure": True}],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)

    assert config.runner_hooks["before_implementing"][0].reject_on_failure is False
    assert config.runner_hooks["after_implementing"][0].command == "echo post"
    assert config.runner_hooks["after_implementing"][0].reject_on_failure is True
    assert config.runner_hook_execution_mode == "run_all"
    assert config.runner_hooks["before_accepting"][0].command == "echo review"
    assert config.runner_hooks["before_accepting"][0].reject_on_failure is False
    assert config.runner_hooks["after_accepting"][0].reject_on_failure is False
    assert config.runner_hooks["after_commit"][0].command == "echo verify"
    assert config.runner_hooks["after_commit"][0].reject_on_failure is True


def test_load_config_preserves_runner_hook_descriptions(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner_hooks": {
                    "after_implementing": [
                        {
                            "command": "uv run ruff check .",
                            "reject_on_failure": True,
                            "description": "ensures lint passes before acceptance",
                        }
                    ]
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.runner_hooks["after_implementing"][0].description == (
        "ensures lint passes before acceptance"
    )


def test_load_config_preserves_runner_hook_execution_mode(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump({"runner_hook_execution_mode": "fail_fast"}, sort_keys=False),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.runner_hook_execution_mode == "fail_fast"


def test_load_config_rejects_invalid_runner_hook_execution_mode(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump({"runner_hook_execution_mode": "sometimes"}, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runner_hook_execution_mode must be one of:"):
        load_config(tmp_path)


def test_configure_rejects_invalid_runner_hook_point(
    tmp_path: Path
) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {"runner_hooks": {"invalid_hook_point": [{"command": "echo nope"}]}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runner_hooks key must be one of:"):
        load_config(tmp_path)
