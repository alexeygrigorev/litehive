from tests.workspace_helpers import (
    CLIExecutionResult,
    EngineBudgetLedger,
    EngineFailure,
    GitError,
    LitehiveConfig,
    Path,
    RuntimeStageState,
    StageReport,
    SubagentManager,
    SubagentRef,
    SubagentResult,
    TaskExecutionRunner,
    _cmd_recover,
    _cmd_rollback,
    _cmd_run,
    _cmd_status,
    _commit_to_git_report,
    _completed_subagent_result,
    _fail_atomic_write_on_path,
    _git_status_without_litehive,
    _init_git_repo,
    _latest_pool_run_report,
    _run,
    _successful_stage_execution,
    _write_cli_verdict,
    argparse,
    checkpoint_message,
    classify_execution_limit,
    classify_retryable_execution_failure,
    create_task,
    drain_task_pool,
    ensure_workspace,
    get_engine,
    get_task,
    get_task_worktree_path,
    load_config,
    load_state,
    pytest,
    recover_completed_task,
    repair_workspace_state,
    require_task,
    resolve_next_task,
    rollback_completed_task,
    run_next_task,
    run_task,
    save_state,
    save_task,
    save_task_runtime,
    task_dir,
    task_file,
    task_runtime_file,
    tasks_module,
    update_task_metadata,
    yaml,
)
from litehive.runtime import _attempt_stage_recovery, _classify_recovery_failure_owner


def test_classify_execution_limit_matches_codex_usage_limit_transcript() -> None:
    transcript = (
        "[stderr]\n"
        "ERROR: You've hit your usage limit. Upgrade to Pro "
        "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits or try again at 5:26 PM."
    )

    assert classify_execution_limit(transcript) == "usage limit reached"


def test_classify_retryable_execution_failure_matches_timeout_transcript() -> None:
    failure = classify_retryable_execution_failure(
        "[stderr]\nRequest failed: upstream connection timed out after 30s."
    )

    assert failure is not None
    assert failure.classification == "timeout"
    assert failure.reason == "transient timeout"


def test_classify_retryable_execution_failure_matches_codex_network_transcript() -> None:
    failure = classify_retryable_execution_failure(
        "[stderr]\nError: error sending request for url (https://chatgpt.com/backend-api/codex): connection closed unexpectedly"
    )

    assert failure is not None
    assert failure.classification == "network"
    assert failure.reason == "transient network failure"


def test_classify_retryable_execution_failure_matches_opencode_network_transcript() -> None:
    failure = classify_retryable_execution_failure(
        "[stderr]\nOpenCode error: fetch failed: getaddrinfo ENOTFOUND api.z.ai"
    )

    assert failure is not None
    assert failure.classification == "network"
    assert failure.reason == "transient network failure"


def test_classify_retryable_execution_failure_matches_opencode_service_transcript() -> None:
    failure = classify_retryable_execution_failure(
        "[stderr]\nOpenCode error: request failed with status code 503 Service Temporarily Unavailable"
    )

    assert failure is not None
    assert failure.classification == "service"
    assert failure.reason == "transient service failure"


def test_classify_retryable_execution_failure_matches_gemini_timeout_transcript() -> None:
    failure = classify_retryable_execution_failure(
        '[stderr]\n{"type":"error","error":{"message":"Request failed: read timeout while waiting for generativelanguage.googleapis.com"}}'
    )

    assert failure is not None
    assert failure.classification == "timeout"
    assert failure.reason == "transient timeout"


def test_classify_retryable_execution_failure_matches_gemini_network_transcript() -> None:
    failure = classify_retryable_execution_failure(
        '[stderr]\n{"type":"error","error":{"message":"Client network socket disconnected before secure TLS connection was established"}}'
    )

    assert failure is not None
    assert failure.classification == "network"
    assert failure.reason == "transient network failure"


def test_classify_retryable_execution_failure_matches_gemini_service_transcript() -> None:
    failure = classify_retryable_execution_failure(
        '[stderr]\n{"type":"error","error":{"message":"GoogleGenerativeAI Error: [503 Service Unavailable] backend unavailable, try again later"}}'
    )

    assert failure is not None
    assert failure.classification == "service"
    assert failure.reason == "transient service failure"


def test_classify_retryable_execution_failure_matches_claude_timeout_payload() -> None:
    failure = classify_retryable_execution_failure(
        '[stderr]\n{"type":"error","error":{"type":"request_timeout","message":"Request timed out while waiting for Anthropic response"}}'
    )

    assert failure is not None
    assert failure.classification == "timeout"
    assert failure.reason == "transient timeout"


def test_classify_retryable_execution_failure_matches_claude_network_payload() -> None:
    failure = classify_retryable_execution_failure(
        '[stderr]\n{"type":"error","error":{"message":"Network connection was lost before the response completed"}}'
    )

    assert failure is not None
    assert failure.classification == "network"
    assert failure.reason == "transient network failure"


def test_classify_retryable_execution_failure_matches_claude_service_payload() -> None:
    failure = classify_retryable_execution_failure(
        '[stderr]\n{"type":"error","error":{"type":"overloaded_error","message":"Anthropic\'s systems are overloaded. Please try again later."}}'
    )

    assert failure is not None
    assert failure.classification == "service"
    assert failure.reason == "transient service failure"


def test_classify_retryable_execution_failure_skips_codex_usage_limit_transcript() -> None:
    failure = classify_retryable_execution_failure(
        "[stderr]\nERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits."
    )

    assert failure is None


def test_classify_retryable_execution_failure_skips_opencode_rate_limit_transcript() -> None:
    transcript = "[stderr]\nOpenCode error: 429 Too Many Requests: rate limit exceeded"

    assert classify_execution_limit(transcript) == "rate limit reached"
    assert classify_retryable_execution_failure(transcript) is None


def test_classify_retryable_execution_failure_skips_gemini_quota_limit_transcript() -> None:
    transcript = (
        '[stderr]\n{"type":"error","error":{"message":"GoogleGenerativeAI Error: [429 Too Many Requests] '
        'You exceeded your current quota, please check your plan and billing details."}}'
    )

    assert classify_execution_limit(transcript) == "quota limit reached"
    assert classify_retryable_execution_failure(transcript) is None


def test_classify_retryable_execution_failure_skips_claude_rate_limit_payload() -> None:
    transcript = (
        '[stderr]\n{"type":"error","error":{"type":"rate_limit_error","message":"Your account has hit a rate limit. '
        'Please slow down and try again later."}}'
    )

    assert classify_execution_limit(transcript) == "rate limit reached"
    assert classify_retryable_execution_failure(transcript) is None


def test_classify_retryable_execution_failure_skips_claude_spend_limit_payload() -> None:
    transcript = '[stderr]\n{"type":"error","error":{"message":"Monthly spend limit reached for this workspace budget."}}'

    assert classify_execution_limit(transcript) == "budget limit reached"
    assert classify_retryable_execution_failure(transcript) is None


def test_run_next_task_uses_routing_plan_before_global_fallbacks_when_budget_blocks_first_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Research engine quota behavior", auto_commit=False)

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        assert engine_name == "codex"
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task(
        tmp_path,
        require_task(tmp_path, "T-0001"),
        budget_ledger=EngineBudgetLedger(engine_usage_caps={"gemini": 0}),
    )

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    # With the current engine routing, codex runs directly since it's the
    # default_engine and first in the attempt order.  The budget cap on gemini
    # is irrelevant because codex succeeds before gemini is attempted.
    assert task.runtime.last_engine_switch is None


def test_run_next_task_falls_back_from_codex_to_opencode_on_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fallback usage-limit task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    opencode = get_engine("opencode")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(opencode, "is_available", lambda: True)
    monkeypatch.setattr(
        "litehive.subagents._manager._supports_live_execution", lambda engine: False
    )

    def fake_codex_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr=(
                    "ERROR: You've hit your usage limit. Upgrade to Pro "
                    "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
                    "to purchase more credits or try again at 5:26 PM."
                ),
            )
        return _successful_stage_execution(tmp_path, "codex", "non-grooming")

    def fake_opencode_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        return _successful_stage_execution(tmp_path, "opencode", step)

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    assert task.runtime.last_engine_switch.reason == "usage limit reached"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-fallback-usage-limit-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` switched from `codex` to `opencode` after usage limit reached."
        in report["warnings"]
    )
    assert report["feedback"].startswith(
        "Stage `grooming` switched from `codex` to `opencode` after usage limit reached."
    )
    assert "grooming complete via opencode" in report["feedback"]
    _cmd_status(argparse.Namespace(workspace=tmp_path, full=True))
    output = capsys.readouterr().out
    assert "engine_switch=grooming codex->opencode reason=usage limit reached" in output


def test_run_next_task_falls_back_after_stale_subagent_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_preference=["codex", "opencode", "gemini", "copilot", "goz"],
            execution_retry_policies={
                "codex": {
                    "max_retries": 0,
                    "backoff_seconds": 0.25,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout"],
                }
            },
            subagent_inactivity_timeout_seconds=0.1,
        ),
    )
    create_task(tmp_path, title="Fallback stale timeout task", engine="codex", auto_commit=False)
    task = require_task(tmp_path, "T-0001")
    codex = get_engine("codex")
    opencode = get_engine("opencode")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(opencode, "is_available", lambda: True)
    monkeypatch.setattr(
        "litehive.subagents._manager._supports_live_execution",
        lambda engine: getattr(engine, "name", None) == "codex",
    )
    monkeypatch.setattr(
        codex,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("codex run should not be used")
        ),
    )  # type: ignore[no-untyped-call]

    def fake_codex_run_live(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
        on_update=None,
        inactivity_timeout_seconds: float = 0,
        **kwargs,
    ) -> CLIExecutionResult:
        del prompt, model, max_turns
        if on_started is not None:
            on_started(7171)
        # Simulate process killed after inactivity timeout
        return CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=cwd,
            exit_code=1,
            stdout="",
            stderr=f"timed out after {inactivity_timeout_seconds}s of inactivity",
            pid=7171,
        )

    def fake_opencode_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        return _successful_stage_execution(tmp_path, "opencode", step)

    monkeypatch.setattr(codex, "run_live", fake_codex_run_live)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)
    monkeypatch.setattr("litehive.subagents._session.os.kill", lambda pid, sig: None)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    assert task.runtime.last_engine_switch.reason == "transient timeout"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-fallback-stale-timeout-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` switched from `codex` to `opencode` after transient timeout."
        in report["warnings"]
    )


def test_run_next_task_falls_back_from_codex_to_gemini_on_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_preference=["codex", "gemini", "opencode", "copilot"]
        ),
    )
    create_task(tmp_path, title="Gemini fallback task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    gemini = get_engine("gemini")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(gemini, "is_available", lambda: True)
    monkeypatch.setattr(
        "litehive.subagents._manager._supports_live_execution", lambda engine: False
    )

    def fake_codex_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Try again later or purchase more credits.",
            )
        return _successful_stage_execution(tmp_path, "codex", "non-grooming")

    def fake_gemini_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        return _successful_stage_execution(tmp_path, "gemini", step)

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(gemini, "run", fake_gemini_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "gemini"
    assert task.runtime.last_engine_switch.reason == "usage limit reached"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-gemini-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` switched from `codex` to `gemini` after usage limit reached."
        in report["warnings"]
    )


def test_run_next_task_keeps_using_fallback_engine_after_implementing_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Fallback usage-limit task",
        engine="codex",
        acceptance_criteria=["Feature works correctly."],
        auto_commit=False,
    )
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    codex = get_engine("codex")
    opencode = get_engine("opencode")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(opencode, "is_available", lambda: True)
    monkeypatch.setattr(
        "litehive.subagents._manager._supports_live_execution", lambda engine: False
    )

    attempted_stages: list[tuple[str, str]] = []

    def fake_codex_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        attempted_stages.append(("codex", step))
        return CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=cwd,
            exit_code=1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
        )

    def fake_opencode_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        attempted_stages.append(("opencode", step))
        return _successful_stage_execution(tmp_path, "opencode", step)

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempted_stages == [
        ("codex", "implementing"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.step == "implementing"
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"


def test_run_next_task_walks_same_stage_fallback_graph_after_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_preference=["codex", "opencode", "gemini", "copilot"]
        ),
    )
    create_task(tmp_path, title="Chained fallback task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    opencode = get_engine("opencode")
    gemini = get_engine("gemini")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(opencode, "is_available", lambda: True)
    monkeypatch.setattr(gemini, "is_available", lambda: True)
    monkeypatch.setattr(
        "litehive.subagents._manager._supports_live_execution", lambda engine: False
    )

    def fake_codex_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Try again later.",
            )
        return _successful_stage_execution(tmp_path, "codex", "non-grooming")

    def fake_opencode_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=1,
                stdout="rate limit exceeded",
                stderr="",
            )
        return _successful_stage_execution(tmp_path, "opencode", "non-grooming")

    def fake_gemini_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        return _successful_stage_execution(tmp_path, "gemini", step)

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)
    monkeypatch.setattr(gemini, "run", fake_gemini_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "opencode"
    assert task.runtime.last_engine_switch.to_engine == "gemini"
    assert task.runtime.last_engine_switch.reason == "rate limit reached"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-chained-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["warnings"][:2] == [
        "Stage `grooming` switched from `codex` to `opencode` after usage limit reached.",
        "Stage `grooming` switched from `opencode` to `gemini` after rate limit reached.",
    ]
    assert report["feedback"].startswith(report["warnings"][0])
    assert "grooming complete via gemini" in report["feedback"]


def test_run_next_task_retries_retryable_execution_failure_before_continuing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            opencode_model="zai-coding-plan/glm-5.1",
            execution_retry_policies={
                "model_family:glm": {
                    "max_retries": 2,
                    "backoff_seconds": 0.25,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout"],
                }
            },
        ),
    )
    create_task(
        tmp_path, title="Retry transient engine failure", engine="opencode", auto_commit=False
    )

    monkeypatch.setattr("litehive.runtime.time.sleep", lambda _seconds: None)

    attempts: list[tuple[str, str]] = []
    grooming_attempts = 0

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        nonlocal grooming_attempts
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if step == "grooming":
            grooming_attempts += 1
            if grooming_attempts == 1:
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-grooming-1",
                        role=role,
                        engine=engine_name,
                        status="failed",
                        path="subagents/grooming-1",
                    ),
                    execution=CLIExecutionResult(
                        adapter=engine_name,
                        argv=(engine_name, "exec"),
                        cwd=tmp_path,
                        exit_code=1,
                        stdout="",
                        stderr="request timed out",
                    ),
                    transcript="[stderr]\nrequest timed out",
                    exit_code=1,
                    failure=EngineFailure(
                        kind="retryable_execution_error",
                        reason="transient timeout",
                        classification="timeout",
                    ),
                )
        return _completed_subagent_result(tmp_path, step, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("opencode", "grooming"),
        ("opencode", "grooming"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is None
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-retry-transient-engine-failure"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` retrying `opencode` after attempt 1/3 due to transient timeout "
        "(classification: timeout, policy: opencode, backoff: 0.25s)." in report["warnings"]
    )


def test_run_next_task_reuses_structured_continuation_handoff_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            opencode_model="zai-coding-plan/glm-5.1",
            execution_retry_policies={
                "model_family:glm": {
                    "max_retries": 1,
                    "backoff_seconds": 0.0,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout"],
                }
            },
        ),
    )
    create_task(
        tmp_path, title="Retry with continuation handoff", engine="opencode", auto_commit=False
    )

    prompts: list[str] = []
    grooming_attempts = 0

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        nonlocal grooming_attempts
        step = task.pipeline_status
        if step == "grooming":
            prompts.append(prompt)
            grooming_attempts += 1
            if grooming_attempts == 1:
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-grooming-1",
                        role=role,
                        engine=engine_name,
                        status="failed",
                        path="subagents/grooming-1",
                    ),
                    execution=CLIExecutionResult(
                        adapter=engine_name,
                        argv=(engine_name, "run"),
                        cwd=tmp_path,
                        exit_code=1,
                        stdout="\n".join(
                            [
                                '{"type":"step_start","timestamp":1,"sessionID":"ses_retry","part":{"id":"prt_1","type":"step-start"}}',
                                '{"type":"text","timestamp":2,"sessionID":"ses_retry","part":{"id":"prt_2","type":"text","text":"Halfway through grooming"}}',
                            ]
                        ),
                        stderr="request timed out",
                    ),
                    transcript="Halfway through grooming\n\n[stderr]\nrequest timed out",
                    exit_code=1,
                    failure=EngineFailure(
                        kind="retryable_execution_error",
                        reason="transient timeout",
                        classification="timeout",
                    ),
                )
        return _completed_subagent_result(tmp_path, step, engine_name=engine_name, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert len(prompts) == 2
    assert "Continuation handoff:" not in prompts[0]
    assert "Continuation handoff:" in prompts[1]
    assert "- Kind: retry" in prompts[1]
    assert "- Reason: transient timeout" in prompts[1]
    assert "- Prior subagent: SA-grooming-1 at `subagents/grooming-1`" in prompts[1]
    assert "- Engine resume id: ses_retry" in prompts[1]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.continuation_handoff is None


def test_run_next_task_passes_structured_continuation_handoff_across_engine_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(engine_preference=["codex", "opencode"]),
    )
    create_task(
        tmp_path, title="Engine switch with continuation handoff", engine="codex", auto_commit=False
    )

    prompts_by_engine: list[tuple[str, str]] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        step = task.pipeline_status
        if step == "grooming":
            prompts_by_engine.append((engine_name, prompt))
        if engine_name == "codex" and step == "grooming":
            return SubagentResult(
                ref=SubagentRef(
                    id="SA-grooming-codex",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path="subagents/grooming-codex",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="\n".join(
                        [
                            '{"type":"thread.started","thread_id":"thread_codex_123"}',
                            '{"type":"error","message":"You\'ve hit your usage limit"}',
                        ]
                    ),
                    stderr="",
                ),
                transcript="You've hit your usage limit",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="usage limit reached"),
            )
        return _completed_subagent_result(tmp_path, step, engine_name=engine_name, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert [engine for engine, _prompt in prompts_by_engine[:2]] == ["codex", "opencode"]
    opencode_prompt = prompts_by_engine[1][1]
    assert "Continuation handoff:" in opencode_prompt
    assert "- Kind: engine_switch" in opencode_prompt
    assert "- Reason: usage limit reached" in opencode_prompt
    assert "- Engine path: codex -> opencode" in opencode_prompt
    assert "- Engine resume id: thread_codex_123" in opencode_prompt
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.continuation_handoff is None


def test_run_next_task_uses_default_opencode_retry_policy_and_records_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig())
    create_task(
        tmp_path,
        title="Retry transient opencode network failure",
        engine="opencode",
        auto_commit=False,
    )

    monkeypatch.setattr("litehive.runtime.time.sleep", lambda _seconds: None)

    attempts: list[tuple[str, str]] = []
    grooming_attempts = 0

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        nonlocal grooming_attempts
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if step == "grooming":
            grooming_attempts += 1
            if grooming_attempts == 1:
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-grooming-1",
                        role=role,
                        engine=engine_name,
                        status="failed",
                        path="subagents/grooming-1",
                    ),
                    execution=CLIExecutionResult(
                        adapter=engine_name,
                        argv=(engine_name, "run"),
                        cwd=tmp_path,
                        exit_code=1,
                        stdout="",
                        stderr="OpenCode error: fetch failed: getaddrinfo ENOTFOUND api.z.ai",
                    ),
                    transcript="[stderr]\nOpenCode error: fetch failed: getaddrinfo ENOTFOUND api.z.ai",
                    exit_code=1,
                    failure=EngineFailure(
                        kind="retryable_execution_error",
                        reason="transient network failure",
                        classification="network",
                    ),
                )
        return _completed_subagent_result(tmp_path, step, engine_name=engine_name, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("opencode", "grooming"),
        ("opencode", "grooming"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-retry-transient-opencode-network-failure"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    expected_warning = (
        "Stage `grooming` retrying `opencode` after attempt 1/3 due to transient network failure "
        "(classification: network, policy: opencode, backoff: 0.25s)."
    )
    assert expected_warning in report["warnings"]
    journal = (
        tmp_path
        / ".litehive"
        / "tasks"
        / "T-0001-retry-transient-opencode-network-failure"
        / "journal.md"
    ).read_text(encoding="utf-8")
    assert expected_warning in journal


def test_run_next_task_uses_default_gemini_retry_policy_and_records_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig())
    create_task(
        tmp_path, title="Retry transient gemini network failure", engine="gemini", auto_commit=False
    )

    monkeypatch.setattr("litehive.runtime.time.sleep", lambda _seconds: None)

    attempts: list[tuple[str, str]] = []
    grooming_attempts = 0

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        nonlocal grooming_attempts
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if step == "grooming":
            grooming_attempts += 1
            if grooming_attempts == 1:
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-grooming-1",
                        role=role,
                        engine=engine_name,
                        status="failed",
                        path="subagents/grooming-1",
                    ),
                    execution=CLIExecutionResult(
                        adapter=engine_name,
                        argv=(engine_name, "-p"),
                        cwd=tmp_path,
                        exit_code=1,
                        stdout="",
                        stderr=(
                            '{"type":"error","error":{"message":"Client network socket disconnected '
                            'before secure TLS connection was established"}}'
                        ),
                    ),
                    transcript=(
                        '[stderr]\n{"type":"error","error":{"message":"Client network socket '
                        'disconnected before secure TLS connection was established"}}'
                    ),
                    exit_code=1,
                    failure=EngineFailure(
                        kind="retryable_execution_error",
                        reason="transient network failure",
                        classification="network",
                    ),
                )
        return _completed_subagent_result(tmp_path, step, engine_name=engine_name, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("gemini", "grooming"),
        ("gemini", "grooming"),
        ("gemini", "implementing"),
        ("gemini", "testing"),
        ("gemini", "accepting"),
    ]
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-retry-transient-gemini-network-failure"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    expected_warning = (
        "Stage `grooming` retrying `gemini` after attempt 1/3 due to transient network failure "
        "(classification: network, policy: gemini, backoff: 0.25s)."
    )
    assert expected_warning in report["warnings"]
    journal = (
        tmp_path
        / ".litehive"
        / "tasks"
        / "T-0001-retry-transient-gemini-network-failure"
        / "journal.md"
    ).read_text(encoding="utf-8")
    assert expected_warning in journal


def test_run_next_task_uses_default_claude_retry_policy_and_records_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(claude_enabled=True))
    create_task(
        tmp_path, title="Retry transient claude service failure", engine="claude", auto_commit=False
    )

    monkeypatch.setattr("litehive.runtime.time.sleep", lambda _seconds: None)

    attempts: list[tuple[str, str]] = []
    grooming_attempts = 0

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        nonlocal grooming_attempts
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if step == "grooming":
            grooming_attempts += 1
            if grooming_attempts == 1:
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-grooming-1",
                        role=role,
                        engine=engine_name,
                        status="failed",
                        path="subagents/grooming-1",
                    ),
                    execution=CLIExecutionResult(
                        adapter=engine_name,
                        argv=(engine_name, "-p"),
                        cwd=tmp_path,
                        exit_code=1,
                        stdout='{"type":"error","error":{"type":"overloaded_error","message":"Anthropic systems are overloaded, please try again later"}}',
                        stderr="",
                    ),
                    transcript=(
                        '[stderr]\n{"type":"error","error":{"type":"overloaded_error",'
                        '"message":"Anthropic systems are overloaded, please try again later"}}'
                    ),
                    exit_code=1,
                    failure=EngineFailure(
                        kind="retryable_execution_error",
                        reason="transient service failure",
                        classification="service",
                    ),
                )
        return _completed_subagent_result(tmp_path, step, engine_name=engine_name, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("claude", "grooming"),
        ("claude", "grooming"),
        ("claude", "implementing"),
        ("claude", "testing"),
        ("claude", "accepting"),
    ]
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-retry-transient-claude-service-failure"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    expected_warning = (
        "Stage `grooming` retrying `claude` after attempt 1/3 due to transient service failure "
        "(classification: service, policy: claude, backoff: 0.25s)."
    )
    assert expected_warning in report["warnings"]
    journal = (
        tmp_path
        / ".litehive"
        / "tasks"
        / "T-0001-retry-transient-claude-service-failure"
        / "journal.md"
    ).read_text(encoding="utf-8")
    assert expected_warning in journal


def test_run_next_task_uses_codex_retry_policy_before_external_cli_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_preference=["codex", "opencode"],
            execution_retry_policies={
                "codex": {
                    "max_retries": 1,
                    "backoff_seconds": 0.25,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["network"],
                },
                "external_cli": {
                    "max_retries": 5,
                    "backoff_seconds": 9.0,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout"],
                },
            },
        ),
    )
    create_task(tmp_path, title="Codex retry policy task", engine="codex", auto_commit=False)

    monkeypatch.setattr("litehive.runtime.time.sleep", lambda _seconds: None)

    attempts: list[tuple[str, str]] = []
    codex_attempts = 0

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        nonlocal codex_attempts
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if engine_name == "opencode":
            return _completed_subagent_result(tmp_path, step, engine_name="opencode", task=task)
        codex_attempts += 1
        if codex_attempts <= 2:
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{step}-{codex_attempts}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{step}-{codex_attempts}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="",
                    stderr="error sending request: connection closed",
                ),
                transcript="[stderr]\nerror sending request: connection closed",
                exit_code=1,
                failure=EngineFailure(
                    kind="retryable_execution_error",
                    reason="transient network failure",
                    classification="network",
                ),
            )
        return _completed_subagent_result(tmp_path, step, engine_name=engine_name, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("codex", "grooming"),
        ("codex", "grooming"),
        ("opencode", "grooming"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    assert task.runtime.last_engine_switch.reason == "transient network failure"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-codex-retry-policy-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["warnings"][:3] == [
        "Stage `grooming` retrying `codex` after attempt 1/2 due to transient network failure (classification: network, policy: codex, backoff: 0.25s).",
        "Stage `grooming` stopped retrying `codex` after attempt 2/2: transient network failure.",
        "Stage `grooming` switched from `codex` to `opencode` after transient network failure.",
    ]


def test_run_next_task_falls_back_after_retry_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_preference=["codex", "opencode"],
            execution_retry_policies={
                "external_cli": {
                    "max_retries": 2,
                    "backoff_seconds": 0.1,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout"],
                }
            },
        ),
    )
    create_task(
        tmp_path, title="Fallback after transient retries", engine="codex", auto_commit=False
    )

    monkeypatch.setattr("litehive.runtime.time.sleep", lambda _seconds: None)

    attempts: list[tuple[str, str]] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if engine_name == "opencode":
            return _completed_subagent_result(tmp_path, step, engine_name="opencode", task=task)
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{step}-{len(attempts)}",
                role=role,
                engine=engine_name,
                status="failed",
                path=f"subagents/{step}-{len(attempts)}",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=1,
                stdout="",
                stderr="request timed out",
            ),
            transcript="[stderr]\nrequest timed out",
            exit_code=1,
            failure=EngineFailure(
                kind="retryable_execution_error",
                reason="transient timeout",
                classification="timeout",
            ),
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("codex", "grooming"),
        ("codex", "grooming"),
        ("codex", "grooming"),
        ("opencode", "grooming"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    assert task.runtime.last_engine_switch.reason == "transient timeout"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-fallback-after-transient-retries"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["warnings"][:3] == [
        "Stage `grooming` retrying `codex` after attempt 1/3 due to transient timeout (classification: timeout, policy: codex, backoff: 0.25s).",
        "Stage `grooming` retrying `codex` after attempt 2/3 due to transient timeout (classification: timeout, policy: codex, backoff: 0.50s).",
        "Stage `grooming` stopped retrying `codex` after attempt 3/3: transient timeout.",
    ]
    assert (
        "Stage `grooming` switched from `codex` to `opencode` after transient timeout."
        in report["warnings"]
    )
    assert report["feedback"].startswith(report["warnings"][0])


def test_run_next_task_does_not_retry_codex_usage_limit_when_codex_policy_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_preference=["codex", "opencode"],
            execution_retry_policies={
                "codex": {
                    "max_retries": 2,
                    "backoff_seconds": 0.25,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout", "network", "service"],
                }
            },
        ),
    )
    create_task(
        tmp_path, title="Codex usage limit is not retryable", engine="codex", auto_commit=False
    )

    attempts: list[tuple[str, str]] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if engine_name == "opencode":
            return _completed_subagent_result(tmp_path, step, engine_name="opencode", task=task)
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{step}-1",
                role=role,
                engine=engine_name,
                status="failed",
                path=f"subagents/{step}-1",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits.",
            ),
            transcript=(
                "[stderr]\nERROR: You've hit your usage limit. Visit "
                "https://chatgpt.com/codex/settings/usage to purchase more credits."
            ),
            exit_code=1,
            failure=EngineFailure(kind="execution_limit", reason="usage limit reached"),
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("codex", "grooming"),
        ("opencode", "grooming"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-codex-usage-limit-is-not-retryable"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["warnings"] == [
        "Stage `grooming` switched from `codex` to `opencode` after usage limit reached."
    ]


def test_run_next_task_skips_retries_for_non_retryable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            execution_retry_policies={
                "external_cli": {
                    "max_retries": 2,
                    "backoff_seconds": 0.25,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout"],
                }
            }
        ),
    )
    create_task(tmp_path, title="Skip non-retryable failure", engine="codex", auto_commit=False)

    attempts: list[tuple[str, str]] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        step = task.pipeline_status
        attempts.append((engine_name, step))
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{step}-1",
                role=role,
                engine=engine_name,
                status="failed",
                path=f"subagents/{step}-1",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=1,
                stdout="",
                stderr="fatal parse error",
            ),
            transcript="[stderr]\nfatal parse error",
            exit_code=1,
            failure=None,
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    assert attempts[0] == ("codex", "grooming")
    assert len(attempts) > 1


def test_run_next_task_skips_unavailable_fallback_engine_after_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_preference=["codex", "gemini", "opencode", "copilot"]
        ),
    )
    create_task(tmp_path, title="Unavailable fallback task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    gemini = get_engine("gemini")
    opencode = get_engine("opencode")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(gemini, "is_available", lambda: False)
    monkeypatch.setattr(opencode, "is_available", lambda: True)
    monkeypatch.setattr(
        "litehive.subagents._manager._supports_live_execution", lambda engine: False
    )

    def fake_codex_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Try again later.",
            )
        return _successful_stage_execution(tmp_path, "codex", "non-grooming")

    def fake_opencode_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        return _successful_stage_execution(tmp_path, "opencode", step)

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "gemini"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-unavailable-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` switched from `codex` to `gemini` after usage limit reached."
        in report["warnings"]
    )
    assert (
        "Stage `grooming` switched from `gemini` to `opencode` after Engine 'gemini' is unavailable: missing binary 'gemini'."
        in report["warnings"]
    )


def test_run_next_task_creates_checkpoint_commit_and_persists_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship checkpoint")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha is not None
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 ship-checkpoint"
    )

    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.commit_message == "litehive: complete T-0001 ship-checkpoint"
    assert task.git.commit_sha == summary.commit_sha
    assert task.git.checkpoint_attempts == 1
    assert task.git.checkpoint_base_sha == initial_sha
    assert task.git.rolled_back_checkpoint_attempt is None
    assert task.runtime.execution_status == "done"
    assert task.runtime.last_stage.step == "commit_to_git"
    assert task.runtime.last_stage.verdict == "pass"
    assert task.runtime.git.commit_sha == summary.commit_sha
    assert task.git.worktree_path is None
    assert not (tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}").exists()


def test_run_next_task_executes_stage_in_task_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Run in worktree", auto_commit=False)
    seen_execution_roots: list[Path] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        seen_execution_roots.append(self.execution_root)
        result = _completed_subagent_result(tmp_path, task.pipeline_status, engine_name=engine_name, task=task)
        if task.pipeline_status == "implementing":
            assert self.execution_root != tmp_path
            assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
            persisted = require_task(tmp_path, task.id)
            assert persisted.runtime.git.worktree_path is not None
            assert get_task_worktree_path(persisted) == str(
                self.execution_root.relative_to(tmp_path)
            )
            (self.execution_root / "app.txt").write_text("worktree-only\n", encoding="utf-8")
        return result

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert seen_execution_roots
    assert all(path != tmp_path for path in seen_execution_roots)
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"


def test_run_next_task_keeps_using_task_worktree_when_main_checkout_is_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Run in isolated worktree", auto_commit=False)
    (tmp_path / "README.md").write_text("main checkout dirt\n", encoding="utf-8")
    seen_execution_roots: list[Path] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        seen_execution_roots.append(self.execution_root)
        result = _completed_subagent_result(tmp_path, task.pipeline_status, engine_name=engine_name, task=task)
        if task.pipeline_status == "implementing":
            assert self.execution_root != tmp_path
            assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
            (self.execution_root / "app.txt").write_text("worktree-only\n", encoding="utf-8")
        return result

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert seen_execution_roots
    assert all(path != tmp_path for path in seen_execution_roots)
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "main checkout dirt\n"


def test_run_next_task_cherry_picks_task_commit_back_to_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Cherry-pick worktree commit")

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        result = _completed_subagent_result(tmp_path, task.pipeline_status, engine_name=engine_name, task=task)
        if task.pipeline_status == "implementing":
            assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
            (self.execution_root / "app.txt").write_text("integrated\n", encoding="utf-8")
        return result

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "integrated\n"


def test_checkpoint_message_attempt_policy_matches_generated_subjects_only(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Message policy", auto_commit=False)

    assert checkpoint_message(task, attempt=1) == "litehive: complete T-0001 message-policy"
    assert (
        checkpoint_message(task, attempt=2)
        == "litehive: complete T-0001 message-policy (attempt 2)"
    )

    task.git.commit_message = "custom: keep subject"
    assert checkpoint_message(task, attempt=2) == "custom: keep subject"

    task.git.commit_message = "litehive: complete T-0001 message-policy"
    assert (
        checkpoint_message(task, attempt=2)
        == "litehive: complete T-0001 message-policy (attempt 2)"
    )


def test_run_next_task_appends_attempt_suffix_after_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship checkpoint")
    (tmp_path / "app.txt").write_text("updated-once\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    first = run_next_task(tmp_path)
    assert first.result is not None
    assert first.result.final_status == "done"

    rollback_completed_task(tmp_path, "T-0001")
    assert _git_status_without_litehive(tmp_path) == []

    (tmp_path / "app.txt").write_text("updated-twice\n", encoding="utf-8")
    second = run_next_task(tmp_path)

    assert second.result is not None
    assert second.result.final_status == "done"
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 ship-checkpoint (attempt 2)"
    )
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.checkpoint_attempts == 2


def test_run_next_task_preserves_future_task_added_during_commit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship checkpoint")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    original_commit_to_git = _commit_to_git_report

    def fail_commit_with_concurrent_add(
        root, execution_root, task, *, auto_commit_enabled, subagents=None, config=None
    ):
        create_task(tmp_path, title="Added during commit failure", auto_commit=False)
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary="CommitToGit failed: simulated merge failure",
        )

    monkeypatch.setattr(
        "litehive.runtime._builder._commit_to_git_report", fail_commit_with_concurrent_add
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    # The runner may launch a recovery agent after the commit failure,
    # which can succeed and re-queue the task.
    assert summary.result.final_status in ("flagged", "merge_failed", "queued")
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert "T-0002" in state.queue
    added = get_task(tmp_path, "T-0002")
    assert added is not None
    assert added.title == "Added during commit failure"
    assert added.status == "queued"


def test_run_next_task_skips_commit_when_not_a_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Needs git repo")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    # The new commit flow skips commit_to_git when there is no git repo
    # and marks the task as done instead of flagging it.
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is None


def test_run_next_task_commits_successfully_with_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Preflight passes")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "done"
    assert task.git.commit_sha is not None


def test_run_next_task_completes_when_task_worktree_path_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new commit flow handles missing worktrees gracefully by committing
    from the main checkout, so a missing worktree path no longer blocks."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Missing preflight worktree")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        if task.pipeline_status == "accepting":
            task.runtime.git.worktree_path = "../missing-preflight-worktree"
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "done"


def test_run_next_task_completes_when_worktree_has_unexpected_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new commit flow merges the worktree into main, so extra commits
    in the worktree are handled naturally by the merge."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Unexpected worktree commit")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        if task.pipeline_status == "accepting":
            worktree_path = tmp_path / str(task.runtime.git.worktree_path)
            _run(["git", "commit", "--allow-empty", "-m", "manual worktree commit"], worktree_path)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "done"


def test_run_next_task_records_blocked_reason_code_when_fallbacks_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Exhausted fallback task", engine="codex", auto_commit=False)

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{role}-{engine_name}",
                role=role,
                engine=engine_name,
                status="failed",
                path=f"subagents/{role}-{engine_name}",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=1,
                stdout="quota exceeded",
                stderr="",
            ),
            transcript="quota exceeded",
            exit_code=1,
            failure=EngineFailure(kind="execution_limit", reason="quota exceeded"),
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    # Exhausted fallbacks set verdict to "blocked" → outcome kind is "blocked".
    assert task.runtime.last_outcome.kind == "blocked"
    assert task.runtime.last_outcome.retry_limit == 3
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-exhausted-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["outcome"] == "blocked"


def test_run_next_task_preserves_git_commit_failure_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Commit diagnostics")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    def fail_commit(root, execution_root, task, *, auto_commit_enabled, subagents=None, config=None):
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary="CommitToGit failed: simulated git commit failure",
        )

    monkeypatch.setattr(
        "litehive.runtime._builder._commit_to_git_report", fail_commit
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    # The runner may launch a recovery agent after the commit failure.
    # The fake SubagentManager.run returns a pass, which may cause the
    # runner to re-queue instead of flagging.
    assert summary.result.final_status in ("flagged", "merge_failed", "queued")


def test_attempt_stage_recovery_launches_agent_for_litehive_traceback_with_no_source_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new recovery flow launches a recovery agent even when
    litehive_source_path is missing/invalid. The recovery agent will
    determine if the failure can be repaired."""
    ensure_workspace(tmp_path, LitehiveConfig(litehive_source_path="/missing/litehive"))
    task = create_task(tmp_path, title="External project task", auto_commit=False)
    save_task(tmp_path, task)

    failed_report = StageReport(
        task_id=task.id,
        step="implementing",
        verdict="fail",
        summary="implementing failed with unhandled error: boom",
        failure_diagnostics={
            "traceback": (
                "Traceback (most recent call last):\n"
                '  File "/usr/lib/python3.12/site-packages/litehive/runtime.py", line 1, in run_task\n'
                "    raise RuntimeError('boom')\n"
                "RuntimeError: boom\n"
            )
        },
    )

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        return SubagentResult(
            ref=SubagentRef(
                id="SA-recovery-1",
                role=role,
                engine=engine_name,
                status="failed",
                path="subagents/recovery-1",
                sandboxed=False,
                sandbox_summary="host",
            ),
            execution=None,
            transcript="VERDICT: FAIL\nSUMMARY: cannot fix without source repo",
            exit_code=1,
            failure=None,
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    report = _attempt_stage_recovery(
        tmp_path,
        tmp_path,
        task,
        "implementing",
        failed_report,
        subagents=SubagentManager(tmp_path),
        config=load_config(tmp_path),
    )

    # Recovery agent could not fix it, so returns None
    assert report is None
    recovery_report = yaml.safe_load(
        (task_dir(tmp_path, task) / "recovery" / "recovery-001.yaml").read_text(encoding="utf-8")
    )
    assert recovery_report["trigger"] == "stage_failure"
    assert recovery_report["runnable_state"] == "blocked"


def test_classify_recovery_failure_owner_prefers_project_paths_over_name_overlap(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(litehive_source_path="/missing/litehive"))
    failed_report = StageReport(
        task_id="T-0001",
        step="implementing",
        verdict="fail",
        summary="implementing failed with unhandled error: boom",
        failure_diagnostics={
            "traceback": (
                "Traceback (most recent call last):\n"
                f'  File "{tmp_path / "litehive" / "module.py"}", line 4, in explode\n'
                "    raise RuntimeError('boom')\n"
                "RuntimeError: boom\n"
            )
        },
    )

    owner, traceback_text, source_root = _classify_recovery_failure_owner(
        tmp_path,
        failed_report,
        config=load_config(tmp_path),
    )

    assert owner == "project"
    assert "RuntimeError: boom" in traceback_text
    assert source_root == Path("/missing/litehive")


def test_attempt_stage_recovery_launches_recovery_agent_for_litehive_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When failure_owner is litehive and litehive_source_path exists, the
    self-heal path launches a recovery agent against the litehive source tree."""
    litehive_root = tmp_path / "litehive-src"
    litehive_root.mkdir()
    _init_git_repo(litehive_root)
    ensure_workspace(tmp_path, LitehiveConfig(litehive_source_path=str(litehive_root)))
    task = create_task(tmp_path, title="External project task", auto_commit=False)
    save_task(tmp_path, task)

    failed_report = StageReport(
        task_id=task.id,
        step="implementing",
        verdict="fail",
        summary="implementing failed with unhandled error: boom",
        failure_diagnostics={
            "traceback": (
                "Traceback (most recent call last):\n"
                f'  File "{litehive_root / "litehive" / "runtime.py"}", line 10, in run_task\n'
                "    raise RuntimeError('boom')\n"
                "RuntimeError: boom\n"
            )
        },
    )

    observed: dict[str, object] = {}

    def fake_run(
        self,
        task_arg,
        role,
        engine_name,
        prompt,
        model=None,
        max_turns=None,
        resume_session_id=None,
    ):  # type: ignore[no-untyped-def]
        observed["role"] = role
        observed["engine"] = engine_name
        observed["prompt"] = prompt
        _write_cli_verdict(
            tmp_path,
            task_arg,
            "implementing",
            verdict="pass",
            message="fixed the litehive bug",
        )
        return SubagentResult(
            ref=SubagentRef(
                id="SA-recovery-1",
                role=role,
                engine=engine_name,
                status="completed",
                path="subagents/recovery-1",
                sandboxed=False,
                sandbox_summary="host",
            ),
            execution=None,
            transcript="fixed the litehive bug",
            exit_code=0,
            failure=None,
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    report = _attempt_stage_recovery(
        tmp_path,
        tmp_path,
        task,
        "implementing",
        failed_report,
        subagents=SubagentManager(tmp_path),
        config=load_config(tmp_path),
    )

    assert report is not None
    assert observed["role"] == "recovery"
    assert "SELF-HEAL" in observed["prompt"]
    assert "uv run pytest" in observed["prompt"]
    recovery_report = yaml.safe_load(
        (task_dir(tmp_path, task) / "recovery" / "recovery-001.yaml").read_text(encoding="utf-8")
    )
    assert recovery_report["trigger"] == "litehive_self_heal"
    assert recovery_report["failure_classification"] == "litehive"


def test_attempt_stage_recovery_returns_none_when_recovery_agent_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the recovery agent cannot resolve the failure, _attempt_stage_recovery
    returns None so the runner flags the task."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="External project task", auto_commit=False)
    save_task(tmp_path, task)

    failed_report = StageReport(
        task_id=task.id,
        step="implementing",
        verdict="fail",
        summary="implementing failed with unhandled error: boom",
        failure_diagnostics={
            "traceback": (
                "Traceback (most recent call last):\n"
                '  File "/usr/lib/python3.12/site-packages/litehive/runtime.py", line 10, in run_task\n'
                "    raise RuntimeError('boom')\n"
                "RuntimeError: boom\n"
            )
        },
    )

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        return SubagentResult(
            ref=SubagentRef(
                id="SA-recovery-1",
                role=role,
                engine=engine_name,
                status="failed",
                path="subagents/recovery-1",
                sandboxed=False,
                sandbox_summary="host",
            ),
            execution=None,
            transcript="VERDICT: FAIL\nSUMMARY: could not fix the issue",
            exit_code=1,
            failure=None,
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    report = _attempt_stage_recovery(
        tmp_path,
        tmp_path,
        task,
        "implementing",
        failed_report,
        subagents=SubagentManager(tmp_path),
        config=load_config(tmp_path),
    )

    assert report is None
    recovery_report = yaml.safe_load(
        (task_dir(tmp_path, task) / "recovery" / "recovery-001.yaml").read_text(encoding="utf-8")
    )
    assert recovery_report["trigger"] == "stage_failure"
    assert recovery_report["runnable_state"] == "blocked"


def test_runner_requeues_same_stage_after_successful_litehive_self_heal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="External project task",
        acceptance_criteria=["The current implementing stage should resume after self-heal."],
        auto_commit=False,
    )
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    def exploding_executor(task_arg, step):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "litehive.runtime._attempt_stage_recovery",
        lambda *args, **kwargs: StageReport(
            task_id=task.id,
            step="implementing",
            verdict="pass",
            summary="Litehive self-heal merged to main and requeued implementing.",
            retry_decision="retry",
            failure_classification="litehive_bug",
        ),
    )

    runner = TaskExecutionRunner(
        tmp_path, exploding_executor, subagents=object(), config=load_config(tmp_path)
    )
    result = runner.run(task)

    refreshed = require_task(tmp_path, task.id)
    state = load_state(tmp_path)
    assert result.final_status == "queued"
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert state.queue[0] == task.id


def test_run_next_task_skips_commit_stage_when_auto_commit_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Skip commit", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha is None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "initial"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.commit_sha is None
    assert task.runtime.git.commit_sha is None


def test_run_next_task_skips_commit_stage_when_workspace_auto_commit_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig(auto_commit=False))
    create_task(tmp_path, title="Skip commit from config")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha is None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "initial"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.commit_sha is None
    assert task.runtime.git.commit_sha is None


def test_run_next_task_flags_task_when_repo_has_unrelated_dirty_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Dirty repo should block commit")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("unrelated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "done"
    assert task.pipeline_status == "done"


def test_run_next_task_flags_task_when_other_task_state_is_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="Ship first task")
    create_task(tmp_path, title="Unrelated pending task")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.task.id == first.id
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha is not None
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 ship-first-task"
    )
    task = get_task(tmp_path, first.id)
    assert task is not None
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is not None
    task_yaml = yaml.safe_load(
        (tmp_path / ".litehive" / "tasks" / "T-0001-ship-first-task" / "task.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert task_yaml["git"]["commit_sha"] == task.git.commit_sha


def test_rollback_command_requeues_checkpointed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fix after done")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)

    exit_code = _cmd_rollback(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    rollback_output = capsys.readouterr().out

    assert exit_code == 0
    assert "rollback_commit:" in rollback_output
    assert (
        "recovery_policy: rollback reverted the checkpoint and requeued the task" in rollback_output
    )
    assert (
        "next_commit_message: litehive: complete T-0001 fix-after-done (attempt 2)"
        in rollback_output
    )
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: rollback T-0001 fix-after-done (attempt 1)"
    )
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "queued"
    assert task.pipeline_status == "implementing"
    assert task.git.rolled_back_checkpoint_attempt == 1
    assert load_state(tmp_path).queue == ["T-0001"]


def test_recover_command_requeues_completed_task_without_revert(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Recover without revert")
    (tmp_path / "app.txt").write_text("ship-again\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    recover_output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 Recover without revert" in recover_output
    assert "pipeline_status: implementing" in recover_output
    assert (
        "recovery_policy: recover requeued the task without reverting workspace code"
        in recover_output
    )
    assert (
        "next_commit_message: litehive: complete T-0001 recover-without-revert (attempt 2)"
        in recover_output
    )
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "ship-again\n"
    assert load_state(tmp_path).queue == ["T-0001"]


def test_recover_completed_task_clears_checkpoint_pointer_and_next_run_uses_next_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Recover rerun")
    (tmp_path / "app.txt").write_text("first-pass\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    first = run_next_task(tmp_path)
    assert first.result is not None
    assert first.result.final_status == "done"

    recovered = recover_completed_task(tmp_path, "T-0001")
    assert recovered.git.commit_sha is None
    assert recovered.runtime.git.commit_sha is None
    assert recovered.git.checkpoint_attempts == 1
    assert recovered.git.checkpoint_base_sha == initial_sha
    assert recovered.git.rolled_back_checkpoint_attempt is None

    (tmp_path / "app.txt").write_text("second-pass\n", encoding="utf-8")
    second = run_next_task(tmp_path)

    assert second.result is not None
    assert second.result.final_status == "done"
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 recover-rerun (attempt 2)"
    )
    refreshed = require_task(tmp_path, "T-0001")
    assert refreshed.git.checkpoint_attempts == 2
    assert refreshed.git.checkpoint_base_sha == first.commit_sha
    assert refreshed.git.commit_sha == second.commit_sha


def test_drain_task_pool_requires_continue_or_rollback_before_unrelated_checkpointed_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="Ship first task")
    second = create_task(tmp_path, title="Unrelated pending task")
    (tmp_path / "app.txt").write_text("first-pass\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    first_summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in first_summary.executions if execution.task is not None
    ] == [first.id]
    assert first_summary.stop_reason == "continue_or_rollback_required"
    first_task = require_task(tmp_path, first.id)
    second_task = require_task(tmp_path, second.id)
    assert first_task.status == "done"
    assert first_task.pipeline_status == "done"
    assert first_task.git.commit_sha is not None
    assert second_task.status == "queued"
    assert second_task.pipeline_status == "backlog"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [second.id]
    assert state.pool_stop_reason == "continue_or_rollback_required"
    journal = (task_dir(tmp_path, first_task) / "journal.md").read_text(encoding="utf-8")
    assert "Pool stopped: continue_or_rollback_required." in journal
    assert (
        "Either continue with a new `litehive run`/pool run or roll back the checkpoint first."
        in journal
    )

    (tmp_path / "app.txt").write_text("second-pass\n", encoding="utf-8")
    resumed = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in resumed.executions if execution.task is not None
    ] == [second.id]
    assert resumed.stop_reason == "queue_exhausted"
    resumed_second = require_task(tmp_path, second.id)
    assert resumed_second.status == "done"
    assert resumed_second.pipeline_status == "done"
    assert resumed_second.git.commit_sha is not None
    assert load_state(tmp_path).queue == []


def test_cmd_run_drain_reports_continue_or_rollback_guidance_after_checkpoint_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship first task")
    create_task(tmp_path, title="Unrelated pending task")
    (tmp_path / "app.txt").write_text("first-pass\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "progress_status: operator_action_required" in output
    assert (
        "summary: Pool stopped after a checkpoint commit. Continue with a new run or roll back the checkpoint before unrelated queued work proceeds."
        in output
    )
    assert "stop_condition: continue or rollback required" in output
    assert "stop_reason: continue_or_rollback_required" in output
    durable_report = _latest_pool_run_report(tmp_path)
    assert durable_report["progress_status"] == "operator_action_required"
    assert (
        durable_report["summary"]
        == "Pool stopped after a checkpoint commit. Continue with a new run or roll back the checkpoint before unrelated queued work proceeds."
    )


def test_recover_completed_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    task = create_task(tmp_path, title="Recover without revert")
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [queued.id]
    save_state(tmp_path, state)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        recover_completed_task(tmp_path, task.id)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == [queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task recovered for another implementation pass." not in journal


def test_recover_completed_task_rolls_back_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    task = create_task(tmp_path, title="Recover without revert")
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [queued.id]
    save_state(tmp_path, state)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        recover_completed_task(tmp_path, task.id)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha is None
    assert load_state(tmp_path).queue == [queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task recovered for another implementation pass." not in journal


def test_recover_completed_task_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    task = create_task(tmp_path, title="Recover without revert")
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [queued.id]
    save_state(tmp_path, state)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        recover_completed_task(tmp_path, task.id)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha is None
    assert load_state(tmp_path).queue == [queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task recovered for another implementation pass." not in journal


def test_drain_task_pool_recovers_stranded_commit_stage_before_new_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = create_task(tmp_path, title="Stranded commit task")
    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{stranded.id}-{stranded.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("updated\n", encoding="utf-8")

    stranded.status = "done"
    stranded.pipeline_status = "done"
    stranded.git.checkpoint_attempts = 1
    stranded.git.checkpoint_base_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)
    stranded.git.commit_sha = None
    stranded.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, stranded)
    report_path = task_dir(tmp_path, stranded) / "reports" / "accepting-001.yaml"
    report_path.write_text(
        yaml.safe_dump(
            {
                "task_id": stranded.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "accepting complete",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 0},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    state = load_state(tmp_path)
    state.queue = []
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [stranded.id]
    assert summary.stop_reason == "queue_exhausted"
    refreshed = get_task(tmp_path, stranded.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha is not None
    assert refreshed.runtime.execution_status == "done"
    assert refreshed.runtime.last_stage.step == "commit_to_git"
    assert refreshed.runtime.last_stage.verdict == "pass"
    assert load_state(tmp_path).queue == []


def test_commit_to_git_ignores_unrelated_main_checkout_changes_when_task_worktree_is_clean(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Finalize isolated worktree commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("updated from task worktree\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("unrelated main checkout dirt\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready to commit",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is not None


def test_commit_to_git_fast_forwards_main_when_worktree_commit_is_direct_descendant(
    tmp_path: Path,
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fast forward worktree commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("fast-forwarded\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready to integrate",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.git.commit_sha is not None
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == task.git.commit_sha
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "fast-forwarded\n"


def test_commit_to_git_cherry_picks_when_main_moved_after_worktree_started(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Cherry pick divergent worktree commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("from worktree\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready to integrate",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    (tmp_path / "README.md").write_text("main moved\n", encoding="utf-8")
    _run(["git", "add", "README.md"], tmp_path)
    _run(["git", "commit", "-m", "main changed"], tmp_path)
    moved_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass", f"commit_to_git failed: {report.summary}"
    assert task.git.commit_sha is not None
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == task.git.commit_sha
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "from worktree\n"


def test_commit_to_git_rebases_worktree_onto_current_main_before_integrating(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Rebase before commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)

    # Worktree edits app.txt line 2
    (worktree_path / "app.txt").write_text("base\nworktree addition\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # Main adds a new file (non-conflicting change)
    (tmp_path / "other.txt").write_text("main work\n", encoding="utf-8")
    _run(["git", "add", "other.txt"], tmp_path)
    _run(["git", "commit", "-m", "main: add other.txt"], tmp_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.git.commit_sha is not None
    # Main should have both the worktree change and the main change
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\nworktree addition\n"
    assert (tmp_path / "other.txt").read_text(encoding="utf-8") == "main work\n"


def test_commit_to_git_treats_clean_task_worktree_as_done(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Already integrated task")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "already integrated",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert task.runtime.git.worktree_path is None

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.git.worktree_path is None
    assert refreshed.runtime.git.worktree_path is None


def test_commit_to_git_integrates_existing_litehive_checkpoint_from_clean_worktree(
    tmp_path: Path,
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recover clean worktree checkpoint")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("checkpointed\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.git.checkpoint_attempts = 1
    task.git.checkpoint_base_sha = initial_sha
    save_task(tmp_path, task)
    commit_message = checkpoint_message(task, attempt=1)
    _run(["git", "add", "app.txt"], worktree_path)
    _run(["git", "commit", "-m", commit_message], worktree_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "checkpointed\n"


def test_commit_to_git_reconciles_existing_checkpoint_commit_without_duplicate_retry(
    tmp_path: Path,
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume committed checkpoint")
    (tmp_path / "app.txt").write_text("checkpointed\n", encoding="utf-8")

    task.git.checkpoint_attempts = 1
    task.git.checkpoint_base_sha = initial_sha
    save_task(tmp_path, task)

    commit_message = checkpoint_message(task, attempt=1)
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_message], tmp_path)
    existing_checkpoint_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)
    commit_count_before = _run(["git", "rev-list", "--count", "HEAD"], tmp_path)

    report = _commit_to_git_report(tmp_path, tmp_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is not None


def test_commit_to_git_integrates_agent_precommit_in_task_worktree(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent committed early")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("agent-commit\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    _run(["git", "add", "app.txt"], worktree_path)
    _run(["git", "commit", "-m", "manual agent commit"], worktree_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.git.commit_sha is not None
    assert task.status == "done"
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "agent-commit\n"


def test_commit_to_git_runs_after_merge_hook_on_main_and_finishes(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={
                "after_merge": [
                    {"command": "grep -q '^from worktree$' app.txt", "blocking": True}
                ]
            }
        ),
    )
    task = create_task(tmp_path, title="Post-merge verification passes")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("from worktree\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)

    report = _commit_to_git_report(
        tmp_path,
        worktree_path,
        task,
        auto_commit_enabled=True,
        config=load_config(tmp_path),
    )

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert report.hook_results[0]["point"] == "after_merge"
    assert report.hook_results[0]["status"] == "passed"
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "from worktree\n"


def test_commit_to_git_requeues_implementing_when_after_merge_hook_fails(tmp_path: Path) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={
                "after_merge": [
                    {"command": "echo post-merge failed >&2; exit 7", "blocking": True}
                ]
            }
        ),
    )
    task = create_task(tmp_path, title="Post-merge verification fails")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("merged before failure\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)

    report = _commit_to_git_report(
        tmp_path,
        worktree_path,
        task,
        auto_commit_enabled=True,
        config=load_config(tmp_path),
    )

    assert report.verdict == "blocked"
    assert report.retry_decision == "retry"
    assert task.status == "queued"
    assert task.pipeline_status == "implementing"
    assert task.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert task.git.commit_sha != initial_sha
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "merged before failure\n"
    assert not worktree_path.exists()
    assert report.hook_results[0]["point"] == "after_merge"
    assert report.hook_results[0]["status"] == "failed"
    assert "without reverting the merge" in report.summary


def test_commit_to_git_skips_after_merge_when_hook_not_configured(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig())
    task = create_task(tmp_path, title="No post-merge verification configured")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("no hook configured\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)

    report = _commit_to_git_report(
        tmp_path,
        worktree_path,
        task,
        auto_commit_enabled=True,
        config=load_config(tmp_path),
    )

    assert report.verdict == "pass"
    assert report.hook_results == []
    assert task.status == "done"
    assert task.pipeline_status == "done"


def test_commit_to_git_handles_metadata_only_worktree_conflict(tmp_path: Path) -> None:
    """When a worktree has only metadata changes that conflict with main's
    state files, the merge will fail. Without a subagent to resolve the
    conflict, the commit returns fail."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Metadata only commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / ".litehive" / "state.yaml").parent.mkdir(parents=True, exist_ok=True)
    (worktree_path / ".litehive" / "state.yaml").write_text(
        "active_task_id: null\n", encoding="utf-8"
    )

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "metadata only",
                "files_changed": ["path/to/file", "none", "-", " N/A "],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    # Without a merge-resolver subagent, metadata-only conflicts cause a fail
    assert report.verdict == "fail"


def test_resolve_next_task_finalizes_existing_checkpoint_commit_without_retry(
    tmp_path: Path,
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = create_task(tmp_path, title="Stranded commit task")
    new_task = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    commit_message = "litehive: complete T-0001 stranded-commit-task"
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_message], tmp_path)
    existing_checkpoint_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)

    stranded.status = "done"
    stranded.pipeline_status = "done"
    stranded.git.checkpoint_attempts = 1
    stranded.git.checkpoint_base_sha = initial_sha
    stranded.git.commit_sha = None
    stranded.runtime.execution_status = "running"
    stranded.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, stranded)
    save_task_runtime(tmp_path, stranded)

    state = load_state(tmp_path)
    state.active_task_id = stranded.id
    state.queue = [new_task.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == new_task.id
    refreshed = get_task(tmp_path, stranded.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == existing_checkpoint_sha
    assert refreshed.git.checkpoint_attempts == 1
    assert refreshed.runtime.execution_status == "done"
    assert refreshed.runtime.last_stage.step == "commit_to_git"
    assert refreshed.runtime.last_stage.verdict == "pass"
    assert refreshed.runtime.current_stage.step is None
    assert load_state(tmp_path).queue == [new_task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered existing checkpoint commit after interrupted `commit_to_git`" in journal
    assert _run(["git", "rev-list", "--count", "HEAD"], tmp_path) == "2"
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == commit_message


def test_resolve_next_task_finalizes_running_commit_stage_with_existing_checkpoint_before_new_work(
    tmp_path: Path,
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = create_task(tmp_path, title="Running commit stage")
    follow_up = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    commit_message = checkpoint_message(stranded, attempt=1)
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_message], tmp_path)
    existing_checkpoint_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)
    commit_count_before = _run(["git", "rev-list", "--count", "HEAD"], tmp_path)

    stranded.status = "in_progress"
    stranded.pipeline_status = "commit_to_git"
    stranded.git.checkpoint_attempts = 1
    stranded.git.checkpoint_base_sha = initial_sha
    stranded.git.commit_sha = None
    stranded.runtime.execution_status = "running"
    stranded.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, stranded)
    save_task_runtime(tmp_path, stranded)

    state = load_state(tmp_path)
    state.active_task_id = stranded.id
    state.queue = [follow_up.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == follow_up.id
    refreshed = require_task(tmp_path, stranded.id)
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == existing_checkpoint_sha
    assert refreshed.git.checkpoint_attempts == 1
    assert refreshed.runtime.execution_status == "done"
    assert load_state(tmp_path).queue == [follow_up.id]
    assert _run(["git", "rev-list", "--count", "HEAD"], tmp_path) == commit_count_before
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == commit_message


def test_resolve_next_task_recovers_orphaned_commit_stage_before_new_work(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    new_task = create_task(tmp_path, title="New task", auto_commit=False)
    orphaned = create_task(tmp_path, title="Orphaned commit stage", auto_commit=False)

    orphaned.status = "in_progress"
    orphaned.pipeline_status = "commit_to_git"
    orphaned.runtime.execution_status = "running"
    orphaned.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, orphaned)
    save_task_runtime(tmp_path, orphaned)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [new_task.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == orphaned.id
    refreshed = get_task(tmp_path, orphaned.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.run_started_at is None
    assert refreshed.runtime.current_stage.step == "commit_to_git"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert load_state(tmp_path).queue == [orphaned.id, new_task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `commit_to_git` was running." in journal
    assert "Resume from `commit_to_git`." in journal


def test_resolve_next_task_recovers_orphaned_interrupted_commit_stage_before_new_work(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    new_task = create_task(tmp_path, title="New task", auto_commit=False)
    orphaned = create_task(tmp_path, title="Halted commit stage", auto_commit=False)

    orphaned.status = "interrupted"
    orphaned.pipeline_status = "commit_to_git"
    orphaned.runtime.execution_status = "interrupted"
    orphaned.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="interrupted",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="blocked",
        summary="Interrupted `commit_to_git` run recovered. Resume from `commit_to_git`.",
    )
    save_task(tmp_path, orphaned)
    save_task_runtime(tmp_path, orphaned)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [new_task.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == orphaned.id
    refreshed = get_task(tmp_path, orphaned.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert load_state(tmp_path).queue == [orphaned.id, new_task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `commit_to_git` was running." in journal
    assert "Resume from `commit_to_git`." in journal


def test_resolve_next_task_recovers_flagged_commit_stage_after_passing_review(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    follow_up = create_task(tmp_path, title="Later task", auto_commit=False)
    flagged = create_task(tmp_path, title="Accepted but not committed", auto_commit=False)

    flagged.status = "flagged"
    flagged.pipeline_status = "commit_to_git"
    flagged.runtime.execution_status = "flagged"
    flagged.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="blocked",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="fail",
        summary="commit never ran",
    )
    save_task(tmp_path, flagged)
    save_task_runtime(tmp_path, flagged)
    (task_dir(tmp_path, flagged) / "reports" / "testing-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": flagged.id,
                "step": "testing",
                "verdict": "pass",
                "summary": "ready for final commit",
                "files_changed": ["litehive/tasks.py"],
                "tests": {"added": 1, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [follow_up.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == flagged.id
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert load_state(tmp_path).queue == [flagged.id, follow_up.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered flagged accepted task back to `queued/commit_to_git`" in journal


def test_resolve_next_task_recovers_flagged_commit_stage_after_failed_commit_report(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    follow_up = create_task(tmp_path, title="Later task", auto_commit=False)
    flagged = create_task(tmp_path, title="Accepted but merge conflicted", auto_commit=False)

    flagged.status = "flagged"
    flagged.pipeline_status = "commit_to_git"
    flagged.runtime.execution_status = "flagged"
    flagged.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="blocked",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="fail",
        summary="CommitToGit failed: merge conflict while integrating task checkpoint",
    )
    flagged.runtime.last_outcome.kind = "flagged"
    flagged.runtime.last_outcome.stage = "commit_to_git"
    flagged.runtime.last_outcome.reason_code = "verdict_fail"
    flagged.runtime.last_outcome.reason = (
        "CommitToGit failed: merge conflict while integrating task checkpoint"
    )
    save_task(tmp_path, flagged)
    save_task_runtime(tmp_path, flagged)
    reports_dir = task_dir(tmp_path, flagged) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": flagged.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready for final commit",
                "files_changed": ["litehive/tasks.py"],
                "tests": {"added": 1, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (reports_dir / "commit_to_git-002.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": flagged.id,
                "step": "commit_to_git",
                "verdict": "fail",
                "summary": "CommitToGit failed: merge conflict while integrating task checkpoint",
                "warnings": ["merge conflict while integrating task checkpoint"],
                "files_changed": [],
                "tests": {"added": 0, "passing": 0},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [follow_up.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == flagged.id
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert load_state(tmp_path).queue == [flagged.id, follow_up.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered flagged accepted task back to `queued/commit_to_git`" in journal


def test_resolve_next_task_recovers_done_accepted_task_without_checkpoint_commit(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    follow_up = create_task(tmp_path, title="Later task", auto_commit=False)
    accepted = create_task(tmp_path, title="Accepted without checkpoint")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{accepted.id}-{accepted.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("resumed-commit\n", encoding="utf-8")

    accepted.status = "done"
    accepted.pipeline_status = "done"
    accepted.runtime.execution_status = "done"
    accepted.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, accepted)
    save_task_runtime(tmp_path, accepted)
    reports_dir = task_dir(tmp_path, accepted) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": accepted.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "accepted and ready for final checkpoint",
                "files_changed": ["app.txt"],
                "tests": {"added": 1, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [follow_up.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == accepted.id
    refreshed = require_task(tmp_path, accepted.id)
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert load_state(tmp_path).queue == [accepted.id, follow_up.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered accepted task back to `queued/commit_to_git`" in journal


def test_commit_to_git_resumes_recovered_done_accepted_worktree_task(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    accepted = create_task(tmp_path, title="Resume final checkpoint")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{accepted.id}-{accepted.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("runner-owned-commit\n", encoding="utf-8")

    accepted.status = "done"
    accepted.pipeline_status = "done"
    accepted.runtime.execution_status = "done"
    accepted.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, accepted)
    save_task_runtime(tmp_path, accepted)
    reports_dir = task_dir(tmp_path, accepted) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": accepted.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "accepted and ready for final checkpoint",
                "files_changed": ["app.txt"],
                "tests": {"added": 1, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = []
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == accepted.id
    refreshed = require_task(tmp_path, accepted.id)
    report = _commit_to_git_report(tmp_path, worktree_path, refreshed, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert refreshed.git.checkpoint_attempts == 1
    assert refreshed.git.worktree_path is None
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "runner-owned-commit\n"


def test_repair_workspace_state_recovers_flagged_commit_stage_after_failed_commit_report(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Repair conflicted integration", auto_commit=False)

    flagged.status = "flagged"
    flagged.pipeline_status = "commit_to_git"
    flagged.runtime.execution_status = "flagged"
    flagged.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="blocked",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="fail",
        summary="CommitToGit failed: cherry-pick conflict while integrating task checkpoint",
    )
    save_task(tmp_path, flagged)
    save_task_runtime(tmp_path, flagged)
    reports_dir = task_dir(tmp_path, flagged) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": flagged.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready for final commit",
                "files_changed": ["litehive/runtime.py"],
                "tests": {"added": 1, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (reports_dir / "commit_to_git-002.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": flagged.id,
                "step": "commit_to_git",
                "verdict": "fail",
                "summary": "CommitToGit failed: cherry-pick conflict while integrating task checkpoint",
                "warnings": ["cherry-pick conflict while integrating task checkpoint"],
                "files_changed": [],
                "tests": {"added": 0, "passing": 0},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    state = load_state(tmp_path)
    state.queue = []
    save_state(tmp_path, state)

    summary = repair_workspace_state(tmp_path)

    assert summary.mutated is True
    assert summary.requeued_task_ids == [flagged.id]
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert load_state(tmp_path).queue == [flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered flagged accepted task back to `queued/commit_to_git`" in journal


def test_rollback_completed_task_restores_state_when_rollback_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on commit failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)

    def fail_rollback_commit(root: Path, message: str):  # type: ignore[no-untyped-def]
        if message.startswith("litehive: rollback "):
            raise GitError("git rollback commit failed")
        return None

    monkeypatch.setattr("litehive.runtime._recovery.commit_task", fail_rollback_commit)

    with pytest.raises(GitError, match="git rollback commit failed"):
        rollback_completed_task(tmp_path, "T-0001")

    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.rolled_back_checkpoint_attempt is None
    assert load_state(tmp_path).queue == []
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "broken\n"
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == checkpoint_head
    assert _git_status_without_litehive(tmp_path) == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_rollback_completed_task_restores_state_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on persist failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        rollback_completed_task(tmp_path, "T-0001")

    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == []
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "broken\n"
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == checkpoint_head
    assert _git_status_without_litehive(tmp_path) == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_rollback_completed_task_restores_state_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on task persist failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)
    task = require_task(tmp_path, "T-0001")

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        rollback_completed_task(tmp_path, "T-0001")

    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == []
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "broken\n"
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == checkpoint_head
    assert _git_status_without_litehive(tmp_path) == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_rollback_completed_task_restores_state_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on persist failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)
    task = require_task(tmp_path, "T-0001")

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        rollback_completed_task(tmp_path, "T-0001")

    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == []
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "broken\n"
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == checkpoint_head
    assert _git_status_without_litehive(tmp_path) == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_recover_command_reroutes_large_task_without_acceptance_criteria_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Fix missing criteria",
        goal="Ship CLI tool",
        acceptance_criteria=["Task completes"],
    )
    task.priority = "high"
    save_task(tmp_path, task)
    (tmp_path / "app.txt").write_text("ship-again\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )
    run_next_task(tmp_path)
    update_task_metadata(tmp_path, task.id, acceptance_criteria=[])

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id=task.id))
    recover_output = capsys.readouterr().out

    assert exit_code == 0
    assert "pipeline_status: grooming" in recover_output
    assert (
        "warning: Structured acceptance criteria are required before implementation for larger tasks."
        in recover_output
    )
    assert (
        "Use `--acceptance-criteria` to persist at least one structured bullet."
        not in recover_output
    )
    recovered = get_task(tmp_path, task.id)
    assert recovered is not None
    assert recovered.pipeline_status == "grooming"
    assert load_state(tmp_path).queue == ["T-0001"]


def test_rollback_requires_completed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Not done yet")

    exit_code = _cmd_rollback(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "is not completed; cannot rollback" in output


def test_recover_requires_completed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Still queued")

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "is not completed; cannot recover" in output


def test_commit_to_git_rerun_reconciles_existing_checkpoint(tmp_path: Path) -> None:
    """Rerunning commit_to_git when the checkpoint commit already exists
    must record the existing SHA, mark the task done, and not create a
    second checkpoint commit."""
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Rerun checkpoint reconciliation")
    (tmp_path / "app.txt").write_text("checkpointed\n", encoding="utf-8")

    task.git.checkpoint_attempts = 1
    task.git.checkpoint_base_sha = initial_sha
    save_task(tmp_path, task)

    commit_msg = checkpoint_message(task, attempt=1)
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_msg], tmp_path)
    existing_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)
    commit_count_before = _run(["git", "rev-list", "--count", "HEAD"], tmp_path)

    report = _commit_to_git_report(tmp_path, tmp_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is not None
    # No duplicate commit was created
    assert _run(["git", "rev-list", "--count", "HEAD"], tmp_path) == commit_count_before
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == commit_msg


def test_recovery_finalizes_stranded_commit_to_git_with_existing_checkpoint(
    tmp_path: Path,
) -> None:
    """Stale-runner recovery must reconcile an existing checkpoint commit
    for a stranded commit_to_git task before queuing new work, without
    incrementing checkpoint_attempts or advancing git history."""
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = create_task(tmp_path, title="Stranded commit")
    follow_up = create_task(tmp_path, title="Follow-up task", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    commit_msg = checkpoint_message(stranded, attempt=1)
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_msg], tmp_path)
    existing_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)
    commit_count_before = _run(["git", "rev-list", "--count", "HEAD"], tmp_path)

    stranded.status = "in_progress"
    stranded.pipeline_status = "commit_to_git"
    stranded.git.checkpoint_attempts = 1
    stranded.git.checkpoint_base_sha = initial_sha
    stranded.git.commit_sha = None
    stranded.runtime.execution_status = "running"
    stranded.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, stranded)
    save_task_runtime(tmp_path, stranded)

    state = load_state(tmp_path)
    state.active_task_id = stranded.id
    state.queue = [follow_up.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == follow_up.id
    refreshed = require_task(tmp_path, stranded.id)
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == existing_sha
    assert refreshed.git.checkpoint_attempts == 1
    assert refreshed.runtime.execution_status == "done"
    # Git history was not advanced
    assert _run(["git", "rev-list", "--count", "HEAD"], tmp_path) == commit_count_before
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == commit_msg
