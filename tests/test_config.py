from tests.workspace_helpers import (
    LitehiveConfig,
    Path,
    _cmd_update,
    argparse,
    build_parser,
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


def test_engine_command_freezes_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import cmd_engine

    exit_code = cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="freeze",
            engine_name="gemini",
            until="2099-01-02",
            reason=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    config = load_config(tmp_path)
    assert config.default_engine == "codex"
    raw_config = yaml.safe_load(
        (tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8")
    )
    assert raw_config["engine_freeze"]["gemini"] == "2099-01-02T00:00:00Z"
    assert "engine_frozen: gemini until 2099-01-02T00:00:00Z" in output


def test_engine_command_parser_accepts_workspace_freeze_args() -> None:
    parser = build_parser()

    args = parser.parse_args(
        ["engine", "freeze", "opencode", "--until", "2099-01-02", "--workspace", "/tmp/demo"]
    )

    assert args.command == "engine"
    assert args.engine_action == "freeze"
    assert args.engine_name == "opencode"
    assert args.until == "2099-01-02"
    assert args.workspace == Path("/tmp/demo")


def test_engine_status_parser_accepts_no_engine_name() -> None:
    parser = build_parser()

    args = parser.parse_args(["engine", "status", "--workspace", "/tmp/demo"])

    assert args.command == "engine"
    assert args.engine_action == "status"
    assert args.engine_name is None
    assert args.workspace == Path("/tmp/demo")


def test_health_parser_accepts_workspace_arg() -> None:
    parser = build_parser()

    args = parser.parse_args(["health", "--workspace", "/tmp/demo"])

    assert args.command == "health"
    assert args.workspace == Path("/tmp/demo")


def test_report_parser_allows_workspace_to_be_omitted() -> None:
    parser = build_parser()

    args = parser.parse_args(["report", "--verdict", "pass", "--message", "ok"])

    assert args.command == "report"
    assert args.workspace is None


def test_report_parser_accepts_repeated_files_changed() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "report",
            "--verdict",
            "pass",
            "--message",
            "ok",
            "--files-changed",
            "foo.py",
            "--files-changed",
            "tests/test_foo.py",
        ]
    )

    assert args.files_changed == ["foo.py", "tests/test_foo.py"]


def test_task_add_parser_accepts_surviving_shaping_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "task",
            "add",
            "Audit CLI surface",
            "--goal",
            "Trim noisy options.",
            "--acceptance-criteria",
            "Keep load-bearing flags.",
            "--depends-on",
            "T-0001,T-0002",
            "--task-type",
            "docs",
            "--mode",
            "single",
            "--priority",
            "high",
            "--workspace",
            "/tmp/demo",
        ]
    )

    assert args.command == "task"
    assert args.task_command == "add"
    assert args.title == "Audit CLI surface"
    assert args.goal == "Trim noisy options."
    assert args.acceptance_criteria == ["Keep load-bearing flags."]
    assert args.depends_on == ["T-0001,T-0002"]
    assert args.task_type == "docs"
    assert args.mode == "single"
    assert args.priority == "high"


@pytest.mark.parametrize(
    "flag",
    [
        "--engine",
        "--model",
        "--retry-limit",
        "--record-mode",
        "--pm-complexity",
        "--planned-effort",
        "--no-auto-commit",
        "--human-checkpoint",
    ],
)
def test_task_add_parser_rejects_removed_bloat_flags(flag: str) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["task", "add", "Trim CLI", flag, "value"])


def test_task_update_parser_accepts_surviving_shaping_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "task",
            "update",
            "T-0001",
            "--title",
            "Renamed task title",
            "--goal",
            "Clarify desired outcome.",
            "--acceptance-criteria",
            "State the done condition.",
            "--constraint",
            "Keep scope local.",
            "--plan-step",
            "Update parser tests.",
            "--depends-on",
            "none",
            "--priority",
            "medium",
            "--from-file",
            "/tmp/task-shape.yaml",
            "--workspace",
            "/tmp/demo",
        ]
    )

    assert args.command == "task"
    assert args.task_command == "update"
    assert args.task_id == "T-0001"
    assert args.title == "Renamed task title"
    assert args.goal == "Clarify desired outcome."
    assert args.acceptance_criteria == ["State the done condition."]
    assert args.constraint == ["Keep scope local."]
    assert args.plan_step == ["Update parser tests."]
    assert args.depends_on == ["none"]
    assert args.priority == "medium"
    assert args.from_file == Path("/tmp/task-shape.yaml")


@pytest.mark.parametrize(
    "argv",
    [
        ["task", "update", "T-0001", "--model", "gpt-5"],
        ["task", "update", "T-0001", "--engine", "gemini"],
        ["task", "update", "T-0001", "--retry-limit", "5"],
        ["task", "update", "T-0001", "--pm-complexity", "moderate"],
        ["task", "update", "T-0001", "--planned-effort", "m"],
        ["task", "update", "T-0001", "--human-checkpoint", "before_acceptance"],
        ["task", "update", "T-0001", "--task-type", "docs"],
        ["task", "update", "T-0001", "--mode", "tasks"],
        ["task", "update", "T-0001", "--auto-commit"],
        ["task", "update", "T-0001", "--no-auto-commit"],
    ],
)
def test_task_update_parser_rejects_removed_bloat_flags(argv: list[str]) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(argv)


def test_import_spec_parser_keeps_engine_and_model_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "import",
            "spec",
            "spec.md",
            "--engine",
            "gemini",
            "--model",
            "gemini-2.5-pro",
            "--workspace",
            "/tmp/demo",
        ]
    )

    assert args.command == "import"
    assert args.import_command == "spec"
    assert args.engine == "gemini"
    assert args.model == "gemini-2.5-pro"
    assert args.file == Path("spec.md")


def test_import_issue_parser_keeps_surviving_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "import",
            "issue",
            "--upstream",
            "Fix upstream timeout handling",
            "--type",
            "engine_adapter_fix",
            "--patch-branch",
            "recover/timeout-fix",
            "--prepare-patch-branch",
            "--workspace",
            "/tmp/demo",
        ]
    )

    assert args.command == "import"
    assert args.import_command == "issue"
    assert args.upstream == "Fix upstream timeout handling"
    assert args.type == "engine_adapter_fix"
    assert args.patch_branch == "recover/timeout-fix"
    assert args.prepare_patch_branch is True


def test_update_command_from_file_still_supports_rich_backdoor_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    task = create_task(tmp_path, title="Rich update fallback")
    payload = tmp_path / "task-shape.yaml"
    payload.write_text(
        yaml.safe_dump(
            {
                "goal": "Route through the rich update backdoor.",
                "task_type": "docs",
                "mode": "tasks",
                "model": "gpt-5",
                "retry_limit": 4,
                "pm_complexity": "moderate",
                "planned_effort": "m",
                "human_checkpoints": ["before_acceptance"],
                "auto_commit": False,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            title=None,
            priority=None,
            goal=None,
            depends_on=None,
            acceptance_criteria=None,
            constraint=None,
            plan_step=None,
            from_file=payload,
            edit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task_type: docs" in output
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.goal == "Route through the rich update backdoor."
    assert updated.task_type == "docs"
    assert updated.mode == "tasks"
    assert updated.model == "gpt-5"
    assert updated.retry_policy.max_retries == 4
    assert updated.pm_complexity == "moderate"
    assert updated.planned_effort == "m"
    assert updated.human_checkpoints == ["before_acceptance"]
    assert updated.git.auto_commit is False


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
    nested = tmp_path / ".litehive" / "worktrees" / task.id / "src"
    nested.mkdir(parents=True)
    (nested.parent / ".litehive").mkdir(parents=True, exist_ok=True)

    from litehive.config import resolve_workspace

    monkeypatch.chdir(nested)
    monkeypatch.setenv("LITEHIVE_TASK_ID", task.id)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)

    assert resolve_workspace(None) == tmp_path.resolve()


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
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Registry lookup")
    config_home = tmp_path / "xdg-config"
    registry = config_home / "litehive" / "workspaces.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        yaml.safe_dump([str(tmp_path)], sort_keys=False),
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()

    from litehive.config import resolve_workspace

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
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

    with pytest.raises(ValueError, match="nested inside another \\.litehive tree"):
        ensure_workspace(nested_root)


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

    from litehive.cli import cmd_engine

    exit_code = cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="status",
            engine_name=None,
        )
    )
    output = capsys.readouterr().out.strip()

    assert exit_code == 0
    assert output.startswith("default_engine: codex | engine_freeze: gemini=2099-06-15T00:00:00Z | engines: ")
    for engine_name in ENGINE_CHOICES:
        assert f"{engine_name}(available=" in output


def test_switch_command_parser_accepts_task_engine_reason_and_workspace() -> None:
    parser = build_parser()

    args = parser.parse_args(
        ["switch", "T-0007", "gemini", "--reason", "quota exhausted", "--workspace", "/tmp/demo"]
    )

    assert args.command == "switch"
    assert args.task_id == "T-0007"
    assert args.engine == "gemini"
    assert args.reason == "quota exhausted"
    assert args.workspace == Path("/tmp/demo")


def test_configure_persists_gemini_model(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="gemini",
        process_profile="generic",
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model="gemini-2.5-pro",
    )

    from litehive.cli import cmd_configure

    assert cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.default_engine == "gemini"
    assert config.gemini_model == "gemini-2.5-pro"


def test_configure_persists_copilot_model(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="copilot",
        process_profile="generic",
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model="gpt-5",
    )

    from litehive.cli import cmd_configure

    assert cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.default_engine == "copilot"
    assert config.copilot_model == "gpt-5"


def test_configure_persists_process_profile(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="rust",
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_dirty_git=False,
    )

    from litehive.cli import cmd_configure

    assert cmd_configure(parser) == 0
    config = load_config(tmp_path)
    context = (tmp_path / ".litehive" / "context.md").read_text(encoding="utf-8")

    assert config.process_profile == "rust"
    assert "# Litehive Workspace Context" in context
    assert "## Rust specifics" in context


def test_load_config_uses_global_defaults_when_workspace_config_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
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

    config = load_config(workspace)

    assert config.default_engine == "gemini"
    assert config.pool_stop_on_failure is True


def test_load_config_applies_workspace_overrides_on_top_of_global_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
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
    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
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
    from litehive.cli import cmd_configure

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=30,
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_dirty_git=False,
        pool_selection_policy="dependency_aware",
    )

    assert cmd_configure(parser) == 0
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
    from litehive.cli import cmd_configure

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=30,
        task_engine_route=None,
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_dirty_git=False,
        pool_selection_policy="dependency_aware",
        pre_acceptance_command=None,
        hook=[
            "before_implementing=run:echo pre",
            "after_implementing=reject:echo post",
            "before_accepting=run:echo review",
            "after_accepting=run:echo accepted",
            "after_commit=reject:echo verify",
        ],
    )

    assert cmd_configure(parser) == 0
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
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from litehive.cli import cmd_configure

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=30,
        task_engine_route=None,
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_dirty_git=False,
        pool_selection_policy="dependency_aware",
        pre_acceptance_command=None,
        hook=["invalid_hook_point=run:echo nope"],
    )

    assert cmd_configure(parser) == 1
    output = capsys.readouterr().out

    assert "configure failed: runner_hooks key must be one of:" in output
