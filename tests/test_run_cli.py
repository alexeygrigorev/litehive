from tests.workspace_helpers import (
    LitehiveConfig,
    Path,
    RuntimeInterruptionState,
    RuntimeStageState,
    RuntimeSubagentState,
    SubagentRef,
    WorkspaceConflictError,
    _block_runner_lock,
    _cmd_run,
    _completed_subagent_result,
    _init_git_repo,
    argparse,
    build_parser,
    create_task,
    drain_task_pool,
    ensure_workspace,
    load_config,
    load_state,
    os,
    pytest,
    require_task,
    run_single_task,
    run_task,
    save_state,
    save_task,
    save_task_runtime,
    task_dir,
    yaml,
)

def test_build_parser_accepts_run_dry_run_flag(tmp_path: Path) -> None:
    parser = build_parser()

    args = parser.parse_args(["run", "--workspace", str(tmp_path), "--dry-run"])

    assert args.command == "run"
    assert args.workspace == tmp_path
    assert args.dry_run is True
    assert args.drain is False
    assert args.engine is None


def test_build_parser_accepts_run_drain_flag(tmp_path: Path) -> None:
    parser = build_parser()

    args = parser.parse_args(["run", "--workspace", str(tmp_path), "--drain"])

    assert args.command == "run"
    assert args.workspace == tmp_path
    assert args.drain is True
    assert args.dry_run is False


def test_build_parser_accepts_model_flags(tmp_path: Path) -> None:
    parser = build_parser()

    add_args = parser.parse_args(
        ["add", "Ship task", "--workspace", str(tmp_path), "--model", "gemini-2.5-pro"]
    )
    run_args = parser.parse_args(["run", "--workspace", str(tmp_path), "--model", "gpt-5"])
    update_args = parser.parse_args(
        ["update", "T-0001", "--workspace", str(tmp_path), "--model", "default"]
    )

    assert add_args.model == "gemini-2.5-pro"
    assert run_args.model == "gpt-5"
    assert update_args.model == "default"


def test_build_parser_accepts_grouped_task_commands(tmp_path: Path) -> None:
    parser = build_parser()

    add_args = parser.parse_args(
        ["task", "add", "Ship task", "--workspace", str(tmp_path), "--model", "gemini-2.5-pro"]
    )
    update_args = parser.parse_args(
        ["task", "update", "T-0001", "--workspace", str(tmp_path), "--model", "default"]
    )
    list_args = parser.parse_args(["task", "list", "--workspace", str(tmp_path)])

    assert add_args.command == "task"
    assert add_args.task_command == "add"
    assert add_args.model == "gemini-2.5-pro"
    assert update_args.command == "task"
    assert update_args.task_command == "update"
    assert update_args.model == "default"
    assert list_args.command == "task"
    assert list_args.task_command == "list"


def test_build_parser_accepts_grouped_queue_commands(tmp_path: Path) -> None:
    parser = build_parser()

    args = parser.parse_args(["queue", "requeue", "T-0001", "--front", "--workspace", str(tmp_path)])

    assert args.command == "queue"
    assert args.queue_command == "requeue"
    assert args.task_id == "T-0001"
    assert args.front is True


def test_build_parser_accepts_grouped_import_commands(tmp_path: Path) -> None:
    parser = build_parser()

    spec_args = parser.parse_args(["import", "spec", "--workspace", str(tmp_path), "notes.md"])
    github_args = parser.parse_args(
        ["import", "github", "--workspace", str(tmp_path), "--repo", "owner/repo", "--all"]
    )

    assert spec_args.command == "import"
    assert spec_args.import_command == "spec"
    assert spec_args.file == Path("notes.md")
    assert github_args.command == "import"
    assert github_args.import_command == "github"
    assert github_args.repo == "owner/repo"
    assert github_args.all is True


def test_build_parser_accepts_runner_lifecycle_commands(tmp_path: Path) -> None:
    parser = build_parser()

    start_args = parser.parse_args(["start", "--workspace", str(tmp_path)])
    stop_args = parser.parse_args(["stop", "--workspace", str(tmp_path)])
    restart_args = parser.parse_args(["restart", "--workspace", str(tmp_path)])

    assert start_args.command == "start"
    assert stop_args.command == "stop"
    assert restart_args.command == "restart"


def test_build_parser_accepts_acceptance_criteria_flags(tmp_path: Path) -> None:
    parser = build_parser()

    add_args = parser.parse_args(
        [
            "add",
            "Ship task",
            "--workspace",
            str(tmp_path),
            "--acceptance-criteria",
            "first criterion",
            "--acceptance-criteria",
            "second criterion",
        ]
    )
    update_args = parser.parse_args(
        [
            "update",
            "T-0001",
            "--workspace",
            str(tmp_path),
            "--acceptance-criteria",
            "none",
        ]
    )

    assert add_args.acceptance_criteria == ["first criterion", "second criterion"]
    assert update_args.acceptance_criteria == ["none"]


def test_build_parser_accepts_pm_sizing_flags(tmp_path: Path) -> None:
    parser = build_parser()

    add_args = parser.parse_args(
        [
            "add",
            "Ship task",
            "--workspace",
            str(tmp_path),
            "--pm-complexity",
            "complex",
            "--planned-effort",
            "l",
        ]
    )
    update_args = parser.parse_args(
        [
            "update",
            "T-0001",
            "--workspace",
            str(tmp_path),
            "--pm-complexity",
            "none",
            "--planned-effort",
            "none",
        ]
    )

    assert add_args.pm_complexity == "complex"
    assert add_args.planned_effort == "l"
    assert update_args.pm_complexity == "none"
    assert update_args.planned_effort == "none"


def test_build_parser_accepts_human_checkpoint_flags(tmp_path: Path) -> None:
    parser = build_parser()

    add_args = parser.parse_args(
        [
            "add",
            "Ship task",
            "--workspace",
            str(tmp_path),
            "--human-checkpoint",
            "before_acceptance",
            "--human-checkpoint",
            "before_commit",
        ]
    )
    update_args = parser.parse_args(
        [
            "update",
            "T-0001",
            "--workspace",
            str(tmp_path),
            "--human-checkpoint",
            "none",
        ]
    )

    assert add_args.human_checkpoint == ["before_acceptance", "before_commit"]
    assert update_args.human_checkpoint == ["none"]


def test_build_parser_accepts_web_monitor_flags(tmp_path: Path) -> None:
    parser = build_parser()

    args = parser.parse_args(
        ["web", "--workspace", str(tmp_path), "--host", "127.0.0.1", "--port", "9001"]
    )

    assert args.command == "web"
    assert args.workspace == tmp_path
    assert args.host == "127.0.0.1"
    assert args.port == 9001


def test_cmd_run_dry_run_shows_planned_tasks_and_stop_conditions_without_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="opencode"))
    create_task(tmp_path, title="Pending task")

    def fail_run_single(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("run_single_task should not be called for dry-run")

    def fail_drain(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("drain_task_pool should not be called for dry-run")

    monkeypatch.setattr("litehive.cli.run.run_single_task", fail_run_single)
    monkeypatch.setattr("litehive.cli.run.drain_task_pool", fail_drain)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "dry_run: true" in output
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 Pending task" in output
    assert "engine=opencode" in output
    assert "engine_attempts=opencode, codex, gemini, copilot" in output
    assert "model=zai-coding-plan/glm-5.1" in output
    assert "human_checkpoints=-" in output
    assert "predicted_stop_condition: single task complete" in output
    assert "predicted_stop_reason: single_task_complete" in output
    assert "stop_on_failure: False" in output
    assert load_state(tmp_path).queue == ["T-0001"]


def test_cmd_run_dry_run_prefers_run_engine_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="opencode"))
    create_task(tmp_path, title="Pending task")

    def fail_run_single(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("run_single_task should not be called for dry-run")

    def fail_drain(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("drain_task_pool should not be called for dry-run")

    monkeypatch.setattr("litehive.cli.run.run_single_task", fail_run_single)
    monkeypatch.setattr("litehive.cli.run.drain_task_pool", fail_drain)

    exit_code = _cmd_run(
        argparse.Namespace(workspace=tmp_path, dry_run=True, engine="gemini", drain=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 Pending task" in output
    assert "engine=gemini" in output
    assert "engine_attempts=gemini, codex, opencode, copilot" in output
    assert "model=-" in output
    assert "human_checkpoints=-" in output
    assert load_state(tmp_path).queue == ["T-0001"]


def test_cmd_run_dry_run_prefers_run_model_override_without_mutating_workspace_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(default_engine="opencode", opencode_model="zai-coding-plan/glm-5.1"),
    )
    create_task(tmp_path, title="Pending task", model="task-model", auto_commit=False)

    config_before = load_config(tmp_path)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=False,
            engine=None,
            model="run-model",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "engine=opencode" in output
    assert "model=run-model" in output
    assert load_config(tmp_path) == config_before


def test_cmd_run_dry_run_plans_dependency_aware_pool_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="opencode"))
    blocked = create_task(tmp_path, title="Blocked dependent", auto_commit=False)
    prerequisite = create_task(tmp_path, title="Prerequisite", auto_commit=False)

    blocked.depends_on = [prerequisite.id]
    save_task(tmp_path, blocked)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 2" in output
    assert "would_run: 1. T-0002 Prerequisite" in output
    assert "would_run: 2. T-0001 Blocked dependent" in output
    assert "blocked_tasks: 0" in output


def test_cmd_run_drain_dry_run_reports_queue_exhausted_without_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="opencode"))
    create_task(tmp_path, title="Pending task", auto_commit=False)

    def fail_drain_task_pool(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("drain_task_pool should not be called for dry-run")

    monkeypatch.setattr("litehive.cli.run.drain_task_pool", fail_drain_task_pool)

    state_before = load_state(tmp_path).model_dump()
    task_before = require_task(tmp_path, "T-0001").model_dump()

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 Pending task" in output
    assert "engine=opencode" in output
    assert "engine_attempts=opencode, codex, gemini, copilot" in output
    assert "predicted_stop_reason: queue_exhausted" in output
    assert load_state(tmp_path).model_dump() == state_before
    assert require_task(tmp_path, "T-0001").model_dump() == task_before
    assert not (tmp_path / ".litehive" / "pool-summary.txt").exists()
    assert not (tmp_path / ".litehive" / "logs" / "pool-runs").exists()


def test_cmd_run_drain_dry_run_reports_empty_queue_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 0" in output
    assert "blocked_tasks: 0" in output
    assert "predicted_stop_reason: queue_exhausted" in output


def test_cmd_run_drain_dry_run_reports_blocked_tasks_remaining_without_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Blocked task", auto_commit=False)
    task.depends_on = ["T-9999"]
    save_task(tmp_path, task)

    state_before = load_state(tmp_path).model_dump()
    task_before = require_task(tmp_path, task.id).model_dump()

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 0" in output
    assert "blocked_tasks: 1" in output
    assert f"blocked: {task.id} Blocked task blocked_by=T-9999 (missing)" in output
    assert "predicted_stop_reason: blocked_tasks_remaining" in output
    assert load_state(tmp_path).model_dump() == state_before
    assert require_task(tmp_path, task.id).model_dump() == task_before


def test_cmd_run_drain_dry_run_reports_dirty_git_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", auto_commit=False)
    _init_git_repo(tmp_path)
    (tmp_path / "app.txt").write_text("dirty\n", encoding="utf-8")

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=True,
            stop_on_dirty_git=True,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 0" in output
    assert "predicted_stop_reason: dirty_git_state" in output


def _mark_interrupted_testing_task(tmp_path: Path, task) -> None:  # type: ignore[no-untyped-def]
    task.status = "interrupted"
    task.pipeline_status = "testing"
    task.acceptance_criteria = ["Resume the interrupted testing stage."]
    task.runtime.execution_status = "interrupted"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="interrupted",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="blocked",
        summary="Execution interrupted. Resume from `testing`.",
    )
    task.runtime.interruption = RuntimeInterruptionState(
        source="runner",
        stage="testing",
        pipeline_status="testing",
        resume_stage="testing",
        reason="Interrupted run recovered after stale runner detection.",
        summary="Resume from `testing`.",
        interrupted_at="2026-04-01T00:01:00+00:00",
        detected_at="2026-04-01T00:01:05+00:00",
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)
    (task_dir(tmp_path, task) / "reports" / "implementing-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "implementing",
                "verdict": "pass",
                "summary": "implemented task changes",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 0},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_cmd_run_drain_dry_run_keeps_dirty_git_stop_for_ambiguous_interrupted_ownership(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    first = create_task(tmp_path, title="First interrupted task", auto_commit=False)
    second = create_task(tmp_path, title="Second interrupted task", auto_commit=False)

    for task in (first, second):
        _mark_interrupted_testing_task(tmp_path, task)

    (tmp_path / "app.txt").write_text("dirty\n", encoding="utf-8")

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=True,
            stop_on_dirty_git=True,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 0" in output
    assert "predicted_stop_reason: dirty_git_state" in output


def test_cmd_run_dry_run_reports_max_tasks_predicted_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(workspace=tmp_path, dry_run=True, max_tasks=1, drain=True)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 First task" in output
    assert "would_run: 2." not in output
    assert "predicted_stop_reason: max_tasks_reached" in output


def test_cmd_run_dry_run_predicts_pool_usage_cap_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(workspace=tmp_path, dry_run=True, pool_usage_cap=1, drain=True)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 First task" in output
    assert "would_run: 2." not in output
    assert "predicted_stop_reason: pool_usage_cap_reached" in output


def test_cmd_run_dry_run_predicts_pool_cost_cap_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=True,
            pool_cost_cap=3,
            engine_cost=["codex=2"],
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 2" in output
    assert "would_run: 1. T-0001 First task" in output
    assert "would_run: 2. T-0002 Second task" in output
    assert "engine=opencode" in output
    assert "predicted_stop_reason: pool_cost_cap_reached" in output


def test_cmd_run_dry_run_predicts_claude_budget_block_without_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="claude",
            engine_budget_caps={"claude": 2},
            engine_costs={"claude": 3},
            engine_preference=[],
        ),
    )
    create_task(tmp_path, title="Claude task", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=False,
            engine=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 0" in output
    assert "predicted_stop_reason: execution_limit_fallbacks_exhausted" in output
    assert "engine_budget_caps: claude=2" in output
    assert "engine_costs: claude=3" in output


def test_cmd_run_dry_run_uses_budget_allowed_fallback_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Research engine quota behavior", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=False,
            engine_usage_cap=["gemini=0"],
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "would_run: 1. T-0001 Research engine quota behavior" in output
    assert "engine=codex" in output
    assert "engine_attempts=codex, opencode, gemini, copilot" in output
    assert "predicted_stop_reason: single_task_complete" in output


def test_drain_task_pool_uses_run_engine_override_for_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", auto_commit=False)
    seen_engines: list[str] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        seen_engines.append(engine_name)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path, engine_override="opencode")

    assert summary.stop_reason == "queue_exhausted"
    assert seen_engines == ["opencode", "opencode", "opencode", "opencode"]


def test_run_single_task_uses_run_engine_override_for_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", auto_commit=False)
    seen_engines: list[str] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        seen_engines.append(engine_name)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    summary = run_single_task(tmp_path, engine_override="opencode")

    assert summary.stop_reason == "single_task_complete"
    assert summary.execution is not None
    assert summary.execution.task is not None
    assert summary.execution.task.id == "T-0001"
    assert seen_engines == ["opencode", "opencode", "opencode", "opencode"]
    assert load_state(tmp_path).queue == []


def test_run_single_task_model_precedence_uses_run_override_then_task_then_workspace_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(default_engine="opencode", opencode_model="workspace-model"),
    )
    create_task(tmp_path, title="Pending task", model="task-model", auto_commit=False)
    seen_models: list[str | None] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        seen_models.append(model)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    run_single_task(tmp_path, model_override="run-model")
    assert seen_models == ["run-model", "run-model", "run-model", "run-model"]

    seen_models.clear()
    create_task(tmp_path, title="Pending task 2", model="task-model-2", auto_commit=False)
    run_single_task(tmp_path)
    assert seen_models == ["task-model-2", "task-model-2", "task-model-2", "task-model-2"]

    seen_models.clear()
    create_task(tmp_path, title="Pending task 3", auto_commit=False)
    run_single_task(tmp_path)
    assert seen_models == [
        "workspace-model",
        "workspace-model",
        "workspace-model",
        "workspace-model",
    ]


def test_run_single_task_does_not_pass_model_override_to_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", model="task-model", auto_commit=False)
    seen_models: list[str | None] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        seen_models.append(model)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    summary = run_single_task(tmp_path, model_override="run-model")

    assert summary.stop_reason == "single_task_complete"
    assert seen_models == [None, None, None, None]


def test_cmd_run_dry_run_budget_overrides_do_not_mutate_workspace_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            pool_usage_cap=8,
            pool_cost_cap=20,
            engine_usage_caps={"codex": 4},
            engine_budget_caps={"claude": 9},
            engine_costs={"codex": 1, "claude": 3},
        ),
    )
    create_task(tmp_path, title="Pending task", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=True,
            engine=None,
            pool_usage_cap=1,
            pool_cost_cap=2,
            engine_usage_cap=["codex=0"],
            engine_budget_cap=["claude=2"],
            engine_cost=["codex=5", "claude=7"],
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "predicted_stop_reason: pool_usage_cap_reached" in output
    assert "pool_usage_cap: 1" in output
    assert "pool_cost_cap: 2" in output
    assert "engine_usage_caps: codex=0" in output
    assert "engine_budget_caps: claude=2" in output
    assert "engine_costs: claude=7, codex=5" in output

    config = load_config(tmp_path)
    assert config.pool_usage_cap == 8
    assert config.pool_cost_cap == 20
    assert config.engine_usage_caps == {"codex": 4}
    assert config.engine_budget_caps == {"claude": 9}
    assert config.engine_costs["codex"] == 1
    assert config.engine_costs["claude"] == 3


def test_drain_task_pool_wraps_pool_execution_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert summary.stop_reason == "queue_exhausted"
    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [
        "T-0001",
        "T-0002",
    ]


def test_run_task_rejects_starting_a_second_active_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task", auto_commit=False)
    pending = create_task(tmp_path, title="Pending task", auto_commit=False)

    active.runtime.execution_status = "running"
    save_task_runtime(tmp_path, active)
    state = load_state(tmp_path)
    state.active_task_id = active.id
    save_state(tmp_path, state)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": os.getpid()}, sort_keys=False),
        encoding="utf-8",
    )
    _block_runner_lock(monkeypatch)

    with pytest.raises(
        WorkspaceConflictError,
        match=f"task {pending.id} cannot start because task {active.id} is already active",
    ):
        run_task(tmp_path, pending)


def _assert_run_task_recovers_stale_active_task_before_conflict_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(
        tmp_path,
        title="Stale active task",
        acceptance_criteria=["Resume from the same stage after stale process recovery."],
        auto_commit=False,
    )
    pending = create_task(
        tmp_path,
        title="Pending task",
        acceptance_criteria=["Run after stale active state is recovered."],
        auto_commit=False,
    )

    active.status = "in_progress"
    active.pipeline_status = "testing"
    active.runtime.execution_status = "running"
    active.runtime.run_started_at = "2026-04-01T00:00:00+00:00"
    active.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    active.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="running",
        path="subagents/SA-0001-qa",
        pid=999999,
        started_at="2026-04-01T00:00:10+00:00",
        updated_at="2026-04-01T00:00:10+00:00",
    )
    active.subagents.append(
        SubagentRef(
            id="SA-0001",
            role="qa",
            engine="codex",
            status="running",
            path="subagents/SA-0001-qa",
        )
    )
    save_task(tmp_path, active)
    save_task_runtime(tmp_path, active)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.pipeline.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_task(tmp_path, pending)

    assert summary.task is not None
    assert summary.task.id == pending.id
    assert summary.result is not None
    assert summary.result.final_status == "done"

    refreshed_active = require_task(tmp_path, active.id)
    assert refreshed_active.status == "interrupted"
    assert refreshed_active.pipeline_status == "testing"
    assert refreshed_active.runtime.execution_status == "interrupted"
    assert refreshed_active.runtime.interruption is not None
    assert refreshed_active.runtime.interruption.resume_stage == "testing"

    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert active.id in restored_state.queue


def test_run_task_recovers_stale_active_task_before_conflict_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_run_task_recovers_stale_active_task_before_conflict_check(tmp_path, monkeypatch)


def test_cli_parser_has_no_duplicate_subcommands_or_arguments() -> None:
    """Catch duplicate subparser or argument definitions that crash argparse."""
    from litehive.cli import build_parser

    # build_parser() raises ArgumentError if any subcommand or argument is duplicated
    parser = build_parser()
    assert parser is not None
