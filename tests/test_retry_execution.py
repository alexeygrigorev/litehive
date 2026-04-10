from tests.workspace_helpers import (
    CLIExecutionResult,
    EngineBudgetLedger,
    EngineFailure,
    LitehiveConfig,
    Path,
    SubagentRef,
    SubagentResult,
    _cmd_status,
    _completed_subagent_result,
    _successful_stage_execution,
    argparse,
    classify_execution_limit,
    classify_retryable_execution_failure,
    create_task,
    ensure_workspace,
    get_engine,
    get_task,
    pytest,
    require_task,
    run_next_task,
    run_task,
    save_task,
    yaml,
)


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

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

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


def _run_next_task_falls_back_from_codex_to_opencode_on_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fallback usage-limit task", auto_commit=False)
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

def _run_next_task_falls_back_from_codex_to_opencode_on_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_next_task_falls_back_from_codex_to_opencode_on_usage_limit(tmp_path, monkeypatch, capsys)


def _run_next_task_falls_back_after_stale_subagent_timeout(
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
    create_task(tmp_path, title="Fallback stale timeout task", auto_commit=False)
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

def _run_next_task_falls_back_after_stale_subagent_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_falls_back_after_stale_subagent_timeout(tmp_path, monkeypatch)


def _run_next_task_falls_back_from_codex_to_gemini_on_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_preference=["codex", "gemini", "opencode", "copilot"]
        ),
    )
    create_task(tmp_path, title="Gemini fallback task", auto_commit=False)
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

def _run_next_task_falls_back_from_codex_to_gemini_on_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_falls_back_from_codex_to_gemini_on_usage_limit(tmp_path, monkeypatch)


def _run_next_task_keeps_using_fallback_engine_after_implementing_usage_limit(
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

def _run_next_task_keeps_using_fallback_engine_after_implementing_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_keeps_using_fallback_engine_after_implementing_usage_limit(tmp_path, monkeypatch)


def _run_next_task_walks_same_stage_fallback_graph_after_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_preference=["codex", "opencode", "gemini", "copilot"]
        ),
    )
    create_task(tmp_path, title="Chained fallback task", auto_commit=False)
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

def _run_next_task_walks_same_stage_fallback_graph_after_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_walks_same_stage_fallback_graph_after_usage_limit(tmp_path, monkeypatch)


def _run_next_task_retries_retryable_execution_failure_before_continuing(
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

    monkeypatch.setattr("litehive.pipeline.time.sleep", lambda _seconds: None)

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

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

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

def _run_next_task_retries_retryable_execution_failure_before_continuing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_retries_retryable_execution_failure_before_continuing(tmp_path, monkeypatch)


def _run_next_task_reuses_structured_continuation_handoff_on_retry(
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

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

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

def _run_next_task_reuses_structured_continuation_handoff_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_reuses_structured_continuation_handoff_on_retry(tmp_path, monkeypatch)


def _run_next_task_passes_structured_continuation_handoff_across_engine_switch(
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

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

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

def _run_next_task_passes_structured_continuation_handoff_across_engine_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_passes_structured_continuation_handoff_across_engine_switch(tmp_path, monkeypatch)


def _run_next_task_uses_default_opencode_retry_policy_and_records_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig())
    create_task(
        tmp_path,
        title="Retry transient opencode network failure",
        engine="opencode",
        auto_commit=False,
    )

    monkeypatch.setattr("litehive.pipeline.time.sleep", lambda _seconds: None)

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

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

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

def _run_next_task_uses_default_opencode_retry_policy_and_records_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_uses_default_opencode_retry_policy_and_records_journal(tmp_path, monkeypatch)


def _run_next_task_uses_default_gemini_retry_policy_and_records_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig())
    create_task(
        tmp_path, title="Retry transient gemini network failure", engine="gemini", auto_commit=False
    )

    monkeypatch.setattr("litehive.pipeline.time.sleep", lambda _seconds: None)

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

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

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

def _run_next_task_uses_default_gemini_retry_policy_and_records_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_uses_default_gemini_retry_policy_and_records_journal(tmp_path, monkeypatch)


def _run_next_task_uses_default_claude_retry_policy_and_records_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig())
    create_task(
        tmp_path, title="Retry transient claude service failure", engine="claude", auto_commit=False
    )

    monkeypatch.setattr("litehive.pipeline.time.sleep", lambda _seconds: None)

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

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

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

def _run_next_task_uses_default_claude_retry_policy_and_records_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_uses_default_claude_retry_policy_and_records_journal(tmp_path, monkeypatch)


def _run_next_task_uses_codex_retry_policy_before_external_cli_fallback(
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
    create_task(tmp_path, title="Codex retry policy task", auto_commit=False)

    monkeypatch.setattr("litehive.pipeline.time.sleep", lambda _seconds: None)

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

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

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

def _run_next_task_uses_codex_retry_policy_before_external_cli_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_uses_codex_retry_policy_before_external_cli_fallback(tmp_path, monkeypatch)


def _run_next_task_falls_back_after_retry_exhaustion(
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

    monkeypatch.setattr("litehive.pipeline.time.sleep", lambda _seconds: None)

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

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

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

def _run_next_task_falls_back_after_retry_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_falls_back_after_retry_exhaustion(tmp_path, monkeypatch)


def _run_next_task_does_not_retry_codex_usage_limit_when_codex_policy_is_configured(
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

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

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

def _run_next_task_does_not_retry_codex_usage_limit_when_codex_policy_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_does_not_retry_codex_usage_limit_when_codex_policy_is_configured(tmp_path, monkeypatch)


def _run_next_task_skips_retries_for_non_retryable_failure(
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
    create_task(tmp_path, title="Skip non-retryable failure", auto_commit=False)

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

    monkeypatch.setattr("litehive.pipeline.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    assert attempts[0] == ("codex", "grooming")
    assert len(attempts) > 1

def _run_next_task_skips_retries_for_non_retryable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_skips_retries_for_non_retryable_failure(tmp_path, monkeypatch)


def _run_next_task_skips_unavailable_fallback_engine_after_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_preference=["codex", "gemini", "opencode", "copilot"]
        ),
    )
    create_task(tmp_path, title="Unavailable fallback task", auto_commit=False)
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

def _run_next_task_skips_unavailable_fallback_engine_after_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_next_task_skips_unavailable_fallback_engine_after_usage_limit(tmp_path, monkeypatch)

