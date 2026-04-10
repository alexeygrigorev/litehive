from tests.workspace_helpers import (
    AdapterCapabilities,
    CLIExecutionResult,
    EngineUsageWindow,
    LitehiveConfig,
    LiveEvent,
    LiveTimeline,
    Path,
    StageReport,
    SubagentManager,
    SubagentRef,
    TaskExecutionRunner,
    _cmd_run,
    _cmd_update,
    _completed_subagent_result,
    argparse,
    create_task,
    ensure_workspace,
    extract_engine_timeline,
    get_engine,
    get_task,
    load_config,
    mark_subagent_started,
    pytest,
    render_task_summary,
    resolve_engine_name,
    run_next_task,
    save_task,
    task_dir,
    tasks_module,
    yaml,
)


def test_engine_registry_uses_adapter_defaults_and_public_lookup_api() -> None:
    from litehive.engines import ENGINE_REGISTRY, get_engine

    assert list(ENGINE_REGISTRY) == ["codex", "opencode", "goz", "gemini", "copilot", "claude"]

    codex = get_engine("codex")
    opencode = get_engine("opencode")

    assert codex.name == "codex"
    assert codex.binary == "codex"
    assert codex.capabilities.supports_model_override is False
    assert codex.capabilities.transcript_format == "jsonl"
    assert opencode.name == "opencode"
    assert opencode.binary == "opencode"
    assert opencode.capabilities.strips_environment is True
    assert "OPENAI_API_KEY" in opencode.stripped_env_vars


def test_adapters_package_does_not_export_private_adapter_internals() -> None:
    import litehive.agents.adapters as adapters

    assert not hasattr(adapters, "_OPENCODE_STRIPPED_ENV_VARS")
    assert not hasattr(adapters, "_CLAUDE_STREAM_EVENT_ADAPTER")
    assert not hasattr(adapters, "_COPILOT_STREAM_EVENT_ADAPTER")
    assert not hasattr(adapters, "_codex_live_events")
    assert not hasattr(adapters, "_opencode_live_events")
    assert not hasattr(adapters, "_gemini_live_events")
    assert not hasattr(adapters, "_claude_live_events")
    assert not hasattr(adapters, "_copilot_live_events")
    assert not hasattr(adapters, "_goz_live_events")


def test_provider_adapter_modules_stay_under_200_lines() -> None:
    adapter_dir = Path(__file__).resolve().parents[1] / "litehive" / "agents" / "adapters"
    for name in ("claude.py", "codex.py", "copilot.py", "gemini.py", "goz.py", "opencode.py"):
        assert len((adapter_dir / name).read_text(encoding="utf-8").splitlines()) < 200


def test_claude_build_invocation_includes_model_and_resume(tmp_path: Path) -> None:
    from litehive.agents import ClaudeCLIAdapter

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )
    invocation = adapter.build_invocation(
        "ship it",
        tmp_path,
        model="claude-sonnet-4-20250514",
    )

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "claude",
        "-p",
        "ship it",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model",
        "claude-sonnet-4-20250514",
    ]

    invocation_resumed = adapter.build_invocation(
        "continue please",
        tmp_path,
        model="claude-sonnet-4-20250514",
        resume_session_id="abc-123",
    )

    assert list(invocation_resumed.argv) == [
        "claude",
        "--resume",
        "abc-123",
        "-p",
        "continue please",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model",
        "claude-sonnet-4-20250514",
    ]


def test_claude_build_invocation_uses_stdin_for_large_prompt(tmp_path: Path) -> None:
    """Prompts exceeding MAX_ARG_STRLEN are piped via stdin, not -p."""
    from litehive.agents import ClaudeCLIAdapter

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )

    large_prompt = "x" * 200_000  # exceeds _MAX_ARG_PROMPT_BYTES

    invocation = adapter.build_invocation(
        large_prompt,
        tmp_path,
        model="claude-sonnet-4-20250514",
    )

    # Prompt must NOT appear in argv
    assert "-p" not in invocation.argv
    assert large_prompt not in invocation.argv
    # Prompt must be available as stdin_data
    assert invocation.stdin_data == large_prompt

    # Resume + large prompt also uses stdin
    invocation_resumed = adapter.build_invocation(
        large_prompt,
        tmp_path,
        model="claude-sonnet-4-20250514",
        resume_session_id="abc-123",
    )
    assert "-p" not in invocation_resumed.argv
    assert "--resume" in invocation_resumed.argv
    assert invocation_resumed.stdin_data == large_prompt

    # Small prompt still uses -p
    small_invocation = adapter.build_invocation("hello", tmp_path)
    assert "-p" in small_invocation.argv
    assert small_invocation.stdin_data is None


def test_codex_build_invocation_uses_exec_for_resume_with_session_context(tmp_path: Path) -> None:
    """codex resume is TUI-only, so resume falls back to exec with session context in prompt."""
    engine = get_engine("codex")

    invocation = engine.build_invocation(
        "continue please",
        tmp_path,
        resume_session_id="abc-123",
    )

    assert invocation.cwd == tmp_path
    argv = list(invocation.argv)
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert "--json" in argv
    assert "[Resuming prior session abc-123]" in argv[-1]
    assert "continue please" in argv[-1]


def test_claude_no_max_turns_by_default(tmp_path: Path) -> None:
    from litehive.agents import ClaudeCLIAdapter

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )
    invocation = adapter.build_invocation("hello", tmp_path)

    assert "--max-turns" not in invocation.argv


def test_claude_build_invocation_includes_max_turns(tmp_path: Path) -> None:
    from litehive.agents import ClaudeCLIAdapter

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )
    invocation = adapter.build_invocation("hello", tmp_path, max_turns=7)

    assert "--max-turns" in invocation.argv
    idx = list(invocation.argv).index("--max-turns")
    assert list(invocation.argv)[idx + 1] == "7"


def test_run_next_task_passes_configured_claude_max_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude", claude_max_turns=7))
    create_task(tmp_path, title="Claude max turns task", auto_commit=False)
    calls: list[int | None] = []

    def fake_subagent_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        calls.append(max_turns)
        return _completed_subagent_result(tmp_path, task.pipeline_status, engine_name="claude", task=task)

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_subagent_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert calls
    assert calls[0] == 7


def test_claude_renders_jsonl_transcript_and_stage_report(tmp_path: Path) -> None:
    from litehive.agents import ClaudeCLIAdapter

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"system","subtype":"init"}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"VERDICT: PASS\\n"}]}}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"SUMMARY: implemented Claude adapter\\n"}]}}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"FILES_CHANGED:\\n- litehive/engines.py\\n"}]}}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"TESTS_ADDED: 3\\nTESTS_PASSING: 3\\nWARNINGS:\\n"}]}}',
                '{"type":"result","result":"done"}',
            ]
        ),
        stderr="",
    )

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )

    assert adapter.render_transcript(execution).splitlines()[0] == "VERDICT: PASS"
    report = adapter.parse_stage_report(
        task_id="T-0006",
        step="implementing",
        execution=execution,
        subagent_status="completed",
    )

    # Text-based verdict parsing removed; without CLI verdict or STAGE_RESULT JSON,
    # verdict defaults to reject.
    assert report.verdict == "reject"
    assert report.summary == "VERDICT: PASS"
    assert report.files_changed == []


def test_claude_renders_partial_stream_events_for_live_capture(tmp_path: Path) -> None:
    from litehive.agents import ClaudeCLIAdapter

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"VERDICT: PASS\\n"}}',
                '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"SUMMARY: partial Claude output\\n"}}',
                '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"FILES_CHANGED:\\n- litehive/engines.py\\n"}}',
                '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"TESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n"}}',
            ]
        ),
        stderr="",
    )

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )

    transcript = adapter.render_transcript(execution)
    assert transcript.splitlines()[0] == "VERDICT: PASS"
    report = adapter.parse_stage_report(
        task_id="T-0006",
        step="implementing",
        execution=execution,
        subagent_status="running",
    )

    assert report.summary == "VERDICT: PASS"
    assert report.files_changed == []


def test_claude_live_progress_report_uses_adapter_summary_for_restart_snippet(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    task = create_task(tmp_path, title="Claude live restart summary", auto_commit=False)
    manager = SubagentManager(tmp_path)

    ref = SubagentRef(
        id="SA-0001",
        role="swe",
        engine="claude",
        status="running",
        path="subagents/SA-0001-swe",
    )
    task.subagents.append(ref)
    mark_subagent_started(tmp_path, task, ref)
    save_task(tmp_path, task)

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    base.mkdir(parents=True, exist_ok=False)
    manager._write_session_start(task, base, ref, "stream partial Claude output")

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"VERDICT: PASS\\n"}}',
                '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"SUMMARY: partial Claude output\\n"}}',
                '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"FILES_CHANGED:\\n- litehive/engines.py\\n"}}',
                '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"TESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n"}}',
            ]
        ),
        stderr="",
        pid=4242,
    )

    manager._write_session_progress(task, base, ref, "stream partial Claude output", execution)

    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert report["status"] == "running"
    assert report["summary"] == "VERDICT: PASS"
    assert report["files_changed"] == []

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    interrupted = tasks_module._mark_interrupted_subagent(
        tmp_path,
        refreshed,
        reason="runner interrupted before subagent completion",
        stage="implementing",
    )

    assert interrupted is not None
    assert interrupted.transcript_snippet == "VERDICT: PASS"

    resumed_report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert resumed_report["status"] == "interrupted"
    assert resumed_report["summary"] == "VERDICT: PASS"
    assert resumed_report["resume_stage"] == "implementing"


def test_claude_stage_report_uses_error_when_no_assistant_message(tmp_path: Path) -> None:
    from litehive.agents import ClaudeCLIAdapter

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout='{"type":"error","data":{"message":"authentication required"}}',
        stderr="",
    )

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )
    report = adapter.parse_stage_report(
        task_id="T-0006",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "authentication required"
    assert report.verdict == "reject"


def test_resolve_engine_name_rejects_claude_when_not_enabled(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    task = create_task(tmp_path, title="Claude task")
    config = load_config(tmp_path)
    assert resolve_engine_name(task, config) == "claude"


def test_resolve_engine_name_rejects_default_claude_when_not_enabled(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    config = load_config(tmp_path)
    assert config.default_engine == "claude"

    task = create_task(tmp_path, title="Claude default task")
    assert resolve_engine_name(task, config) == "claude"


def test_resolve_engine_name_allows_claude_when_enabled(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    task = create_task(tmp_path, title="Claude task")
    config = load_config(tmp_path)
    assert resolve_engine_name(task, config) == "claude"


def test_claude_is_not_default_engine() -> None:
    config = LitehiveConfig()
    assert config.default_engine != "claude"


def test_claude_config_defaults_to_sonnet() -> None:
    config = LitehiveConfig()
    assert config.claude_model == "claude-sonnet-4-20250514"
    assert config.claude_max_turns == 100


def test_claude_not_in_engine_preference() -> None:
    config = LitehiveConfig()
    assert "claude" not in config.engine_preference, "claude should not be in default engine_preference"


def test_claude_engine_in_registry() -> None:
    engine = get_engine("claude")
    assert engine.name == "claude"
    assert engine.capabilities.supports_model_override is True
    assert engine.capabilities.transcript_format == "jsonl"


def test_goz_engine_in_registry() -> None:
    engine = get_engine("goz")
    assert engine.name == "goz"
    assert engine.capabilities.supports_model_override is True
    assert engine.capabilities.transcript_format == "jsonl"


def test_goz_render_transcript_joins_streaming_text_and_formats_tool_blocks(tmp_path: Path) -> None:
    adapter = get_engine("goz")
    execution = CLIExecutionResult(
        adapter="goz",
        argv=("goz", "run", "--format", "json"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"text","part":{"text":"I am checking observ"}}',
                '{"type":"text","part":{"text":"ability and writing a "}}',
                '{"type":"text","part":{"text":"readable transcript.\\n\\n"}}',
                '{"type":"tool_use","part":{"id":"call_1","name":"bash","input":{"command":"pwd"},"output":"/tmp/work","is_error":false}}',
                '{"type":"text","part":{"text":"The command finished and "}}',
                '{"type":"text","part":{"text":"the sentence stays intact."}}',
            ]
        ),
        stderr="",
    )

    transcript = adapter.render_transcript(execution)

    assert "observability and writing a readable transcript." in transcript
    assert "observ\nability" not in transcript
    assert "```tool\nname: bash\nid: call_1" in transcript
    assert 'input:\n{\n  "command": "pwd"\n}' in transcript
    assert "output:\n/tmp/work" in transcript
    assert "The command finished and the sentence stays intact." in transcript


def test_goz_extract_usage_observation_reads_tokens_and_cost(tmp_path: Path) -> None:
    adapter = get_engine("goz")

    observation = adapter.extract_usage_observation(
        CLIExecutionResult(
            adapter="goz",
            argv=("goz", "run", "--format", "json"),
            cwd=tmp_path,
            exit_code=0,
            stdout="\n".join(
                [
                    '{"type":"message","role":"assistant","content":"done"}',
                    '{"type":"usage","usage":{"input_tokens":120,"output_tokens":30,"total_tokens":150,"model":"glm-4.5"},"cost":{"total_usd":0.0123}}',
                ]
            ),
            stderr="",
        )
    )

    assert observation is not None
    assert observation.provider == "z.ai"
    assert observation.success is True
    assert observation.usage == EngineUsageWindow(used=150, unit="tokens")
    assert observation.metadata["input_tokens"] == 120
    assert observation.metadata["output_tokens"] == 30
    assert observation.metadata["total_tokens"] == 150
    assert observation.metadata["model"] == "glm-4.5"
    assert observation.metadata["cost"] == "0.012300"


def test_update_command_rejects_removed_claude_engine_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig())
    task = create_task(tmp_path, title="Tune Claude task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="claude",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert "update failed: no changes requested" in output


def test_update_command_rejects_removed_goz_engine_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune Goz task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="goz",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert "update failed: no changes requested" in output


def test_configure_persists_claude_settings(tmp_path: Path) -> None:
    from litehive.cli import _cmd_configure

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=20,
        pool_usage_cap=12,
        pool_cost_cap=30,
        engine_usage_cap=["claude=2", "codex=5"],
        engine_budget_cap=["claude=6"],
        engine_cost=["claude=3", "codex=1"],
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
    )

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.claude_model == "claude-sonnet-4-20250514"
    assert config.claude_max_turns == 20
    assert config.pool_usage_cap == 12
    assert config.pool_cost_cap == 30
    assert config.engine_usage_caps == {"claude": 2, "codex": 5}
    assert config.engine_budget_caps == {"claude": 6}
    assert config.engine_costs["claude"] == 3


def test_configure_updates_existing_workspace_budget_settings(tmp_path: Path) -> None:
    from litehive.cli import _cmd_configure

    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            process_profile="generic",
            pool_usage_cap=4,
            pool_cost_cap=8,
            engine_usage_caps={"codex": 2},
            engine_budget_caps={"claude": 3},
            engine_costs={"codex": 1, "claude": 3},
        ),
    )

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="python",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=20,
        pool_usage_cap=12,
        pool_cost_cap=30,
        engine_usage_cap=["claude=2", "codex=5"],
        engine_budget_cap=["claude=6"],
        engine_cost=["claude=3", "codex=1"],
        task_engine_route=None,
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
        pool_selection_policy="dependency_aware",
    )

    assert _cmd_configure(parser) == 0

    config = load_config(tmp_path)
    assert config.process_profile == "python"
    assert config.claude_max_turns == 20
    assert config.pool_usage_cap == 12
    assert config.pool_cost_cap == 30
    assert config.engine_usage_caps == {"claude": 2, "codex": 5}
    assert config.engine_budget_caps == {"claude": 6}
    assert config.engine_costs["claude"] == 3
    assert config.engine_costs["codex"] == 1
    assert config.engine_costs["opencode"] == 1
    assert config.engine_costs["gemini"] == 1
    assert config.engine_costs["copilot"] == 1
    context = (tmp_path / ".litehive" / "context.md").read_text(encoding="utf-8")
    assert "## Python specifics" in context


def test_claude_model_resolved_from_workspace_defaults() -> None:
    from litehive.pipeline import workspace_model_for_engine

    config = LitehiveConfig(claude_model="claude-sonnet-4-20250514")
    assert workspace_model_for_engine(config, "claude") == "claude-sonnet-4-20250514"

    config_default = LitehiveConfig()
    assert workspace_model_for_engine(config_default, "claude") == "claude-sonnet-4-20250514"


def test_cmd_run_dry_run_rejects_default_claude_when_not_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Claude default task")

    def fail_run_single(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("run_single_task should not be called for dry-run")

    def fail_drain(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("drain_task_pool should not be called for dry-run")

    monkeypatch.setattr("litehive.cli.run.run_single_task", fail_run_single)
    monkeypatch.setattr("litehive.cli.run.drain_task_pool", fail_drain)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, engine=None))
    assert exit_code == 0


def test_cmd_run_dispatches_single_task_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    called: list[str] = []

    def fake_run_single(*args, **kwargs):  # type: ignore[no-untyped-def]
        called.append("single")
        return 0

    def fail_run_drain(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("drain handler should not run for single-task mode")

    monkeypatch.setattr("litehive.cli.run._cmd_run_single", fake_run_single)
    monkeypatch.setattr("litehive.cli.run._cmd_run_drain", fail_run_drain)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))

    assert exit_code == 0
    assert called == ["single"]


def test_cmd_run_dispatches_pool_drain_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    called: list[str] = []

    def fail_run_single(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("single-task handler should not run for drain mode")

    def fake_run_drain(*args, **kwargs):  # type: ignore[no-untyped-def]
        called.append("drain")
        return 0

    monkeypatch.setattr("litehive.cli.run._cmd_run_single", fail_run_single)
    monkeypatch.setattr("litehive.cli.run._cmd_run_drain", fake_run_drain)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=True))

    assert exit_code == 0
    assert called == ["drain"]


def test_extract_engine_timeline_returns_none_for_empty_stdout() -> None:
    result = extract_engine_timeline("opencode", "")
    assert result is None


def test_extract_engine_timeline_returns_none_for_blank_stdout() -> None:
    result = extract_engine_timeline("opencode", "   \n  \n  ")
    assert result is None


def test_extract_engine_timeline_returns_none_when_no_events_extracted() -> None:
    result = extract_engine_timeline("opencode", "not jsonl at all\njust plain text")
    assert result is None


def test_extract_engine_timeline_opencode_message_events() -> None:
    stdout = '{"type":"text","part":{"text":"VERDICT: PASS\\nSUMMARY: did the thing"}}\n'
    timeline = extract_engine_timeline("opencode", stdout, task_id="T-0001", subagent_id="SA-0001")
    assert timeline is not None
    assert timeline.engine == "opencode"
    assert timeline.task_id == "T-0001"
    assert timeline.subagent_id == "SA-0001"
    assert len(timeline.events) == 1
    assert timeline.events[0].kind == "message"
    assert timeline.events[0].role == "assistant"
    assert "VERDICT: PASS" in timeline.events[0].content
    assert timeline.event_counts == {"message": 1}


def test_extract_engine_timeline_opencode_usage_events() -> None:
    stdout = '{"type":"step_finish","part":{"tokens":{"total":100,"input":60,"output":40},"cost":0.001}}\n'
    timeline = extract_engine_timeline("opencode", stdout)
    assert timeline is not None
    assert len(timeline.events) == 1
    assert timeline.events[0].kind == "usage"
    assert timeline.events[0].metadata.get("total_tokens") == 100


def test_extract_engine_timeline_codex_message_events() -> None:
    stdout = '{"type":"item.completed","item":{"type":"agent_message","text":"I did the work"}}\n'
    timeline = extract_engine_timeline("codex", stdout, task_id="T-0042")
    assert timeline is not None
    assert timeline.engine == "codex"
    assert timeline.task_id == "T-0042"
    assert len(timeline.events) == 1
    assert timeline.events[0].kind == "message"
    assert timeline.events[0].content == "I did the work"


def test_extract_engine_timeline_gemini_content_events() -> None:
    stdout = '{"type":"content","text":"Hello from Gemini"}\n'
    timeline = extract_engine_timeline("gemini", stdout)
    assert timeline is not None
    assert timeline.engine == "gemini"
    assert len(timeline.events) == 1
    assert timeline.events[0].kind == "message"
    assert timeline.events[0].content == "Hello from Gemini"


def test_extract_engine_timeline_claude_delta_events() -> None:
    stdout = '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi from Claude"}}\n'
    timeline = extract_engine_timeline("claude", stdout)
    assert timeline is not None
    assert timeline.engine == "claude"
    assert len(timeline.events) == 1
    assert timeline.events[0].kind == "message"
    assert timeline.events[0].content == "Hi from Claude"


def test_extract_engine_timeline_copilot_message_events() -> None:
    stdout = '{"type":"assistant.message","data":{"content":"Copilot response"}}\n'
    timeline = extract_engine_timeline("copilot", stdout)
    assert timeline is not None
    assert timeline.engine == "copilot"
    assert len(timeline.events) == 1
    assert timeline.events[0].kind == "message"
    assert timeline.events[0].content == "Copilot response"


def test_extract_engine_timeline_goz_message_and_usage_events() -> None:
    stdout = (
        '{"type":"assistant.message_delta","delta":"Thinking..."}\n'
        '{"type":"usage","usage":{"input_tokens":75,"output_tokens":25,"total_tokens":100},"cost":{"total_usd":0.0015}}\n'
    )
    timeline = extract_engine_timeline("goz", stdout, task_id="T-0010", subagent_id="SA-0003")
    assert timeline is not None
    assert timeline.engine == "goz"
    assert timeline.task_id == "T-0010"
    assert timeline.subagent_id == "SA-0003"
    assert len(timeline.events) == 2
    assert timeline.events[0].kind == "message"
    assert timeline.events[0].content == "Thinking..."
    assert timeline.events[1].kind == "usage"
    assert timeline.events[1].metadata["total_tokens"] == 100
    assert timeline.events[1].metadata["cost"] == "0.001500"
    assert timeline.event_counts == {"message": 1, "usage": 1}


def test_extract_engine_timeline_error_events() -> None:
    stdout = (
        '{"type":"error","error":{"name":"RateLimitError","data":{"message":"rate limit hit"}}}\n'
    )
    timeline = extract_engine_timeline("opencode", stdout)
    assert timeline is not None
    assert len(timeline.events) == 1
    assert timeline.events[0].kind == "error"
    assert "rate limit hit" in timeline.events[0].error


def test_extract_engine_timeline_mixed_events() -> None:
    stdout = (
        '{"type":"text","part":{"text":"thinking..."}}\n'
        '{"type":"step_finish","part":{"tokens":{"total":50,"input":30,"output":20}}}\n'
    )
    timeline = extract_engine_timeline("opencode", stdout)
    assert timeline is not None
    assert len(timeline.events) == 2
    assert timeline.event_counts == {"message": 1, "usage": 1}


def test_live_timeline_model_task_and_subagent_fields() -> None:
    timeline = LiveTimeline(
        engine="opencode",
        task_id="T-0001",
        subagent_id="SA-0001",
        events=[
            LiveEvent(kind="message", engine="opencode", role="assistant", content="hello"),
        ],
    )
    timeline.recompute_counts()
    data = timeline.model_dump(mode="python")
    assert data["task_id"] == "T-0001"
    assert data["subagent_id"] == "SA-0001"
    assert data["event_counts"] == {"message": 1}


def test_subagent_writes_timeline_on_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Timeline finish test")
    manager = SubagentManager(tmp_path)

    stdout = '{"type":"text","part":{"text":"VERDICT: PASS\\nSUMMARY: done"}}\n'

    class FakeEngine:
        name = "opencode"
        binary = "opencode"

        def is_available(self) -> bool:
            return True

        def run(self, prompt, cwd, model=None, *, max_turns=None, on_started=None):
            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout=stdout,
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution):
            return execution.stdout

    monkeypatch.setattr("litehive.subagents._manager.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="opencode", prompt="do it")

    assert result.ref.status == "completed"
    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    timeline_path = base / "timeline.yaml"
    assert timeline_path.exists()
    timeline_data = yaml.safe_load(timeline_path.read_text(encoding="utf-8"))
    assert timeline_data["engine"] == "opencode"
    assert timeline_data["task_id"] == task.id
    assert timeline_data["subagent_id"] == "SA-0001"
    assert len(timeline_data["events"]) == 1
    assert timeline_data["events"][0]["kind"] == "message"


def test_subagent_writes_timeline_during_live_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Timeline live progress test")
    manager = SubagentManager(tmp_path)

    partial_stdout = '{"type":"text","part":{"text":"partial output"}}\n'

    class FakeStreamingEngine:
        name = "opencode"
        binary = "opencode"

        def is_available(self) -> bool:
            return True

        def run_live(
            self,
            prompt,
            cwd,
            model=None,
            *,
            max_turns=None,
            on_update=None,
            inactivity_timeout_seconds=0,
            **kwargs,
        ):
            first = CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout=partial_stdout,
                stderr="",
                pid=5151,
            )
            if on_update is not None:
                on_update(first)

            base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
            timeline_path = base / "timeline.yaml"
            assert timeline_path.exists()
            timeline_data = yaml.safe_load(timeline_path.read_text(encoding="utf-8"))
            assert timeline_data["engine"] == "opencode"
            assert timeline_data["task_id"] == task.id
            assert len(timeline_data["events"]) == 1

            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout=partial_stdout + '{"type":"step_finish","part":{"tokens":{"total":50}}}\n',
                stderr="",
                pid=5151,
            )

        def render_transcript(self, execution):
            return execution.transcript

    monkeypatch.setattr("litehive.subagents._manager.get_engine", lambda _: FakeStreamingEngine())

    result = manager.run(task, role="swe", engine_name="opencode", prompt="stream it")
    assert result.ref.status == "completed"

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    timeline_path = base / "timeline.yaml"
    assert timeline_path.exists()
    timeline_data = yaml.safe_load(timeline_path.read_text(encoding="utf-8"))
    assert len(timeline_data["events"]) == 2
    assert timeline_data["event_counts"] == {"message": 1, "usage": 1}


def test_subagent_skips_timeline_when_no_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="No timeline test")
    manager = SubagentManager(tmp_path)

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run(self, prompt, cwd, model=None, *, max_turns=None, on_started=None):
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="plain text output with no jsonl",
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution):
            return execution.stdout

    monkeypatch.setattr("litehive.subagents._manager.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="no events")
    assert result.ref.status == "completed"

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    assert not (base / "timeline.yaml").exists()


def test_runner_persists_duration_seconds_in_report_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Duration tracking task")

    def executor(task, step):  # type: ignore[no-untyped-def]
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor)
    runner.run(task)

    reports_dir = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    for report_path in reports_dir.glob("*.yaml"):
        data = yaml.safe_load(report_path.read_text(encoding="utf-8"))
        assert "duration_seconds" in data, f"duration_seconds missing in {report_path.name}"
        assert isinstance(data["duration_seconds"], int)
        assert data["duration_seconds"] >= 0


def test_render_task_summary_includes_estimate_velocity_and_eta(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Estimate demo task")

    # Manually create a report with a known duration to seed the velocity estimate.
    reports_dir = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "grooming-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "grooming",
                "verdict": "pass",
                "summary": "ok",
                "duration_seconds": 120,
            }
        ),
        encoding="utf-8",
    )

    task.pipeline_status = "implementing"
    lines = render_task_summary(task, active=True, root=tmp_path)
    combined = "\n".join(lines)
    assert "stage_estimate=" in combined
    assert "velocity=" in combined
    assert "eta=" in combined
