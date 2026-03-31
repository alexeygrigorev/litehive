import argparse
from pathlib import Path
import subprocess

import pytest
import yaml

from litehive.cli import (
    _cmd_add,
    _cmd_move,
    _cmd_promote,
    _cmd_queue,
    _cmd_recover,
    _cmd_requeue_task,
    _cmd_rollback,
    _cmd_run,
    _cmd_status,
    _cmd_update,
)
from litehive.engines import classify_execution_limit, get_engine
from litehive.config import LitehiveConfig, ensure_workspace, load_config, resolve_process_profile
from litehive.external_cli import CLIExecutionResult, parse_stage_report_text
from litehive.models import RuntimeStageState, RuntimeSubagentState, StageReport, SubagentRef
from litehive.runtime import (
    TaskPoolStopConditions,
    resolve_engine_name,
    resolve_next_task,
    run_next_task,
    run_task_pool,
)
from litehive.runner import TaskExecutionRunner
from litehive.subagents import EngineFailure, SubagentResult, stage_prompt, stage_report_from_subagent
from litehive.tasks import (
    create_task,
    dequeue_next_task_selection,
    get_task,
    list_tasks,
    load_state,
    move_queued_task,
    peek_next_task_selection,
    save_state,
    save_task,
    set_active_task,
)


def test_ensure_workspace_creates_layout(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    assert (tmp_path / ".litehive" / "config.yaml").exists()
    assert (tmp_path / ".litehive" / "state.yaml").exists()
    assert (tmp_path / ".litehive" / "tasks").exists()


def test_ensure_workspace_scaffolds_profile_specific_context(tmp_path: Path) -> None:
    django_path = tmp_path / "django"
    django_path.mkdir()

    from litehive.config import LitehiveConfig

    ensure_workspace(django_path, LitehiveConfig(process_profile="django"))

    context = (django_path / ".litehive" / "context.md").read_text(encoding="utf-8")

    assert "Process profile: Django" in context
    assert "## Init scaffold" in context
    assert "## Prompt scaffold" in context
    assert "## Django specifics" in context
    assert "migrations" in context
    assert "Shared stages: grooming -> implementing -> testing -> accepting -> commit_to_git." in context


def test_resolve_process_profile_merges_shared_process_with_overlay() -> None:
    profile = resolve_process_profile("codehive")

    assert profile["label"] == "Codehive-style"
    assert profile["shared_stages"] == [
        "grooming",
        "implementing",
        "testing",
        "accepting",
        "commit_to_git",
    ]
    assert profile["orchestrator_model"] == "the orchestrator is the manager; subagents execute but do not choose routing."
    assert profile["routing_model"].startswith("manager-owned deterministic routing")
    assert any("generic base prompt" in line for line in profile["prompt_scaffold"])
    assert profile["stage_overlay"]["accepting"][0].startswith("- Acceptance is managerial review")


def test_create_task_persists_folder_and_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Fix login race")
    tasks = list_tasks(tmp_path)
    state = load_state(tmp_path)

    assert task.id == "T-0001"
    assert len(tasks) == 1
    assert state.queue == ["T-0001"]
    assert (tmp_path / ".litehive" / "tasks" / "T-0001-fix-login-race" / "task.yaml").exists()


def test_runner_advances_task_to_done(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Implement feature")

    def executor(task, step):  # type: ignore[no-untyped-def]
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    reports = tmp_path / ".litehive" / "tasks" / "T-0001-implement-feature" / "reports"
    assert (reports / "grooming-001.yaml").exists()
    assert (reports / "implementing-002.yaml").exists()
    assert (reports / "testing-003.yaml").exists()
    assert (reports / "accepting-004.yaml").exists()
    assert (reports / "commit_to_git-005.yaml").exists()


def test_runner_fails_task_when_retry_limit_is_exhausted(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=1))
    task = create_task(tmp_path, title="Retry exhausted")

    def executor(task, step):  # type: ignore[no-untyped-def]
        verdict = "fail" if step == "testing" else "pass"
        return StageReport(task_id=task.id, step=step, verdict=verdict, summary=f"{step} {verdict}")

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=1)
    result = runner.run(task)

    assert result.final_status == "failed"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "failed"
    assert task.runtime.retry_count == 2
    assert task.runtime.retry_limit == 1
    assert task.runtime.last_outcome.kind == "failed"
    assert task.runtime.last_outcome.stage == "testing"
    assert task.runtime.last_outcome.reason == "Retry limit exhausted after testing fail: testing fail"
    report = yaml.safe_load(
        (tmp_path / ".litehive" / "tasks" / "T-0001-retry-exhausted" / "reports" / "testing-005.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert report["retry_count"] == 2
    assert report["retry_limit"] == 1
    assert report["retry_decision"] == "final"
    assert report["outcome"] == "failed"
    assert report["outcome_reason"] == "Retry limit exhausted after testing fail: testing fail"


def test_runner_cancels_task_with_explicit_reason(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Cancelled run")

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            raise KeyboardInterrupt()
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "cancelled"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "cancelled"
    assert task.runtime.last_outcome.kind == "cancelled"
    assert task.runtime.last_outcome.stage == "testing"
    assert task.runtime.last_outcome.reason == "Execution cancelled during testing"
    report = yaml.safe_load(
        (tmp_path / ".litehive" / "tasks" / "T-0001-cancelled-run" / "reports" / "testing-003.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert report["retry_decision"] == "final"
    assert report["outcome"] == "cancelled"
    assert report["outcome_reason"] == "Execution cancelled during testing"


def test_runner_fails_task_when_stage_executor_crashes(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Executor crash")

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            raise RuntimeError("boom")
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "failed"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "failed"
    assert task.runtime.last_outcome.kind == "failed"
    assert task.runtime.last_outcome.stage == "testing"
    assert task.runtime.last_outcome.reason == "testing failed with unhandled error: boom"
    report = yaml.safe_load(
        (tmp_path / ".litehive" / "tasks" / "T-0001-executor-crash" / "reports" / "testing-003.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert report["warnings"] == ["boom"]
    assert report["retry_decision"] == "final"
    assert report["outcome"] == "failed"
    assert report["outcome_reason"] == "testing failed with unhandled error: boom"


def test_run_next_task_uses_task_retry_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    task = create_task(tmp_path, title="Override retry limit", auto_commit=False)
    task.retry_policy.max_retries = 1
    save_task(tmp_path, task)
    attempts = {"testing": 0}

    def fake_run(self, task, role, engine_name, prompt, model=None):  # type: ignore[no-untyped-def]
        if task.pipeline_status == "testing":
            attempts["testing"] += 1
            if attempts["testing"] == 1:
                transcript = "\n".join(
                    [
                        "VERDICT: FAIL",
                        "SUMMARY: tests failed once",
                        "FILES_CHANGED:",
                        "TESTS_ADDED: 0",
                        "TESTS_PASSING: 0",
                        "WARNINGS:",
                    ]
                )
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-testing-codex",
                        role=role,
                        engine=engine_name,
                        status="completed",
                        path="subagents/testing-codex",
                    ),
                    execution=CLIExecutionResult(
                        adapter=engine_name,
                        argv=(engine_name, "exec"),
                        cwd=tmp_path,
                        exit_code=0,
                        stdout=transcript,
                        stderr="",
                    ),
                    transcript=transcript,
                    exit_code=0,
                )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.runtime.retry_limit == 1
    assert task.runtime.retry_count == 1
    assert task.runtime.retry_source == "task"
    assert task.runtime.last_outcome.kind is None
    report = yaml.safe_load(
        (tmp_path / ".litehive" / "tasks" / "T-0001-override-retry-limit" / "reports" / "testing-003.yaml")
        .read_text(encoding="utf-8")
    )
    assert report["retry_count"] == 1
    assert report["retry_limit"] == 1
    assert report["retry_source"] == "task"
    assert report["retry_decision"] == "retry"


def test_opencode_strips_provider_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {}

    def fake_run(cmd, cwd, capture_output, text, env, check):  # type: ignore[no-untyped-def]
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        calls["env"] = env
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/fake")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("OPENCODE_API_KEY", "secret2")

    engine = get_engine("opencode")
    result = engine.run("hello", tmp_path)

    assert result.returncode == 0
    assert calls["cwd"] == str(tmp_path)
    assert list(calls["cmd"]) == ["opencode", "run", "--dir", str(tmp_path), "hello"]
    assert "OPENAI_API_KEY" not in calls["env"]
    assert "OPENCODE_API_KEY" not in calls["env"]


def test_gemini_build_invocation_includes_model_and_jsonl_flags(tmp_path: Path) -> None:
    invocation = get_engine("gemini").build_invocation(
        "ship it",
        tmp_path,
        model="gemini-2.5-pro",
    )

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "gemini",
        "-p",
        "ship it",
        "--output-format",
        "stream-json",
        "--yolo",
        "-m",
        "gemini-2.5-pro",
    ]    


def test_copilot_build_invocation_includes_model_and_jsonl_flags(tmp_path: Path) -> None:
    invocation = get_engine("copilot").build_invocation(
        "ship it",
        tmp_path,
        model="gpt-5",
    )

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "copilot",
        "-p",
        "ship it",
        "--output-format",
        "json",
        "--allow-all-tools",
        "--autopilot",
        "--no-auto-update",
        "--add-dir",
        str(tmp_path),
        "--model",
        "gpt-5",
    ]


def test_engine_capabilities_report_availability_and_contract_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")

    codex = get_engine("codex").detect_capabilities()
    opencode = get_engine("opencode").detect_capabilities()
    gemini = get_engine("gemini").detect_capabilities()
    copilot = get_engine("copilot").detect_capabilities()

    assert codex.available is True
    assert codex.supports_model_override is False
    assert codex.transcript_format == "text"
    assert opencode.available is True
    assert opencode.supports_model_override is True
    assert opencode.strips_environment is True
    assert gemini.available is True
    assert gemini.supports_model_override is True
    assert gemini.transcript_format == "jsonl"
    assert copilot.available is True
    assert copilot.supports_model_override is True
    assert copilot.transcript_format == "jsonl"


def test_codex_build_invocation_includes_workspace_and_prompt(tmp_path: Path) -> None:
    invocation = get_engine("codex").build_invocation("ship it", tmp_path)

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        str(tmp_path),
        "--skip-git-repo-check",
        "ship it",
    ]


def test_opencode_build_invocation_includes_dir_model_and_prompt(tmp_path: Path) -> None:
    invocation = get_engine("opencode").build_invocation(
        "ship it",
        tmp_path,
        model="zai-coding-plan/glm-5.1",
    )

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "opencode",
        "run",
        "--dir",
        str(tmp_path),
        "--model",
        "zai-coding-plan/glm-5.1",
        "ship it",
    ]


def test_gemini_renders_jsonl_transcript_and_stage_report(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="gemini",
        argv=("gemini", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"init","session_id":"abc","model":"gemini-2.5-pro"}',
                '{"type":"message","role":"assistant","content":"VERDICT: PASS\\n","delta":true}',
                '{"type":"message","role":"assistant","content":"SUMMARY: implemented Gemini adapter\\n","delta":true}',
                '{"type":"message","role":"assistant","content":"FILES_CHANGED:\\n- litehive/engines.py\\n","delta":true}',
                '{"type":"message","role":"assistant","content":"TESTS_ADDED: 4\\nTESTS_PASSING: 4\\nWARNINGS:\\n","delta":true}',
                '{"type":"result","status":"success"}',
            ]
        ),
        stderr="",
    )

    engine = get_engine("gemini")

    assert engine.render_transcript(execution).splitlines()[0] == "VERDICT: PASS"
    report = engine.parse_stage_report(
        task_id="T-0004",
        step="implementing",
        execution=execution,
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert report.summary == "implemented Gemini adapter"
    assert report.files_changed == ["litehive/engines.py"]
    assert report.tests == {"added": 4, "passing": 4}


def test_gemini_stage_report_uses_tool_error_when_no_assistant_message(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="gemini",
        argv=("gemini", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout='{"type":"tool_result","status":"error","error":{"message":"permission denied"}}',
        stderr="",
    )

    report = get_engine("gemini").parse_stage_report(
        task_id="T-0004",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "permission denied"
    assert report.verdict == "blocked"


def test_copilot_renders_jsonl_transcript_and_stage_report(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"assistant.turn_start","data":{"turnId":"0"}}',
                '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"VERDICT: PASS\\n"},"ephemeral":true}',
                '{"type":"assistant.message","data":{"messageId":"m1","content":"VERDICT: PASS\\nSUMMARY: implemented Copilot adapter\\nFILES_CHANGED:\\n- litehive/engines.py\\nTESTS_ADDED: 2\\nTESTS_PASSING: 2\\nWARNINGS:\\n"}}',
                '{"type":"result","exitCode":0}',
            ]
        ),
        stderr="",
    )

    engine = get_engine("copilot")

    assert engine.render_transcript(execution).splitlines()[0] == "VERDICT: PASS"
    report = engine.parse_stage_report(
        task_id="T-0005",
        step="implementing",
        execution=execution,
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert report.summary == "implemented Copilot adapter"
    assert report.files_changed == ["litehive/engines.py"]
    assert report.tests == {"added": 2, "passing": 2}


def test_copilot_stage_report_uses_json_error_when_no_assistant_message(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout='{"type":"error","data":{"message":"authentication required"}}',
        stderr="",
    )

    report = get_engine("copilot").parse_stage_report(
        task_id="T-0005",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "authentication required"
    assert report.verdict == "blocked"


def test_copilot_render_transcript_falls_back_to_message_deltas(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"assistant.turn_start","data":{"turnId":"0"}}',
                '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"VERDICT: PASS\\n"},"ephemeral":true}',
                '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"SUMMARY: streamed only\\n"}}',
                '{"type":"assistant.turn_end","data":{"turnId":"0"}}',
            ]
        ),
        stderr="",
    )

    transcript = get_engine("copilot").render_transcript(execution)

    assert transcript == "VERDICT: PASS\nSUMMARY: streamed only"


def test_copilot_stage_report_uses_failed_tool_result_when_no_message(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout=(
            '{"type":"tool.execution_complete","data":{"toolName":"write","success":false,'
            '"result":{"content":"disk full"}}}'
        ),
        stderr="",
    )

    report = get_engine("copilot").parse_stage_report(
        task_id="T-0005",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "disk full"
    assert report.verdict == "blocked"


def test_execution_result_transcript_combines_stdout_and_stderr(tmp_path: Path) -> None:
    result = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=tmp_path,
        exit_code=1,
        stdout="SUMMARY: failed\n",
        stderr="missing binary",
    )

    assert result.transcript == "SUMMARY: failed\n\n[stderr]\nmissing binary"


def test_parse_stage_report_text_extracts_shared_report_fields() -> None:
    report = parse_stage_report_text(
        task_id="T-0003",
        step="implementing",
        transcript=(
            "VERDICT: PASS\n"
            "SUMMARY: adapter contract added\n"
            "FILES_CHANGED:\n"
            "- litehive/engines.py\n"
            "- litehive/external_cli.py\n"
            "TESTS_ADDED: 3\n"
            "TESTS_PASSING: 8\n"
            "WARNINGS:\n"
            "- kept claude deferred\n"
        ),
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert report.summary == "adapter contract added"
    assert report.files_changed == ["litehive/engines.py", "litehive/external_cli.py"]
    assert report.tests == {"added": 3, "passing": 8}
    assert report.warnings == ["kept claude deferred"]


def test_stage_report_from_subagent_uses_adapter_execution_transcript(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Adapter task")
    result = SubagentResult(
        ref=SubagentRef(
            id="SA-0001",
            role="swe",
            engine="codex",
            status="completed",
            path="subagents/SA-0001-swe",
        ),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=tmp_path,
            exit_code=0,
            stdout="VERDICT: PASS\nSUMMARY: execution transcript parsed",
            stderr="",
        ),
        transcript="ignored fallback transcript",
        exit_code=0,
    )

    report = stage_report_from_subagent(task, "implementing", result)

    assert report.summary == "execution transcript parsed"
    assert report.verdict == "pass"


def test_stage_prompt_includes_shared_process_and_profile_overlay(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Profiled task", goal="Ship deterministic routing")
    prompt = stage_prompt(
        task,
        "testing",
        workspace_context="## Project\n- Purpose: validate overlays",
        process_profile="codehive",
    )

    assert "Process profile: Codehive-style" in prompt
    assert "Shared stages: grooming -> implementing -> testing -> accepting -> commit_to_git." in prompt
    assert "Routing model: manager-owned deterministic routing, retries, and escalation stay in local code rather than prompts." in prompt
    assert "the orchestrator is the manager; subagents execute but do not choose routing." in prompt
    assert "Combine the generic base prompt with the selected project overlay instead of replacing the base." in prompt
    assert "Verification should be independent enough to catch behavioral regressions" in prompt
    assert "default to regression-first or test-first implementation" in prompt


def test_run_next_task_no_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    summary = run_next_task(tmp_path)

    assert summary.task is None
    assert summary.result is None


def test_run_task_pool_no_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    summary = run_task_pool(tmp_path)

    assert summary.executions == []
    assert summary.stop_reason == "queue_exhausted"


def test_run_task_pool_drains_dynamic_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None):  # type: ignore[no-untyped-def]
        if task.id == first.id and get_task(tmp_path, "T-0002") is None:
            create_task(tmp_path, title="Second task", auto_commit=False)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task_pool(tmp_path)

    assert [execution.task.id for execution in summary.executions if execution.task is not None] == [
        "T-0001",
        "T-0002",
    ]
    assert summary.stop_reason == "queue_exhausted"
    assert load_state(tmp_path).active_task_id is None
    assert load_state(tmp_path).queue == []
    second = get_task(tmp_path, "T-0002")
    assert second is not None
    assert second.status == "done"


def test_run_next_task_falls_back_to_next_engine_on_execution_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fallback task", engine="codex", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None):  # type: ignore[no-untyped-def]
        if engine_name == "codex":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="rate limit exceeded",
                    stderr="",
                ),
                transcript="rate limit exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="rate limit reached"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    report = yaml.safe_load(
        (tmp_path / ".litehive" / "tasks" / "T-0001-fallback-task" / "reports" / "grooming-001.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert "Stage `grooming` switched from `codex` to `opencode` after rate limit reached." in report["warnings"]


def test_run_next_task_flags_when_limit_fallbacks_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Exhausted fallback task", engine="codex", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None):  # type: ignore[no-untyped-def]
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{task.pipeline_status}-{engine_name}",
                role=role,
                engine=engine_name,
                status="failed",
                path=f"subagents/{task.pipeline_status}-{engine_name}",
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
    assert task.status == "flagged"
    report = yaml.safe_load(
        (tmp_path / ".litehive" / "tasks" / "T-0001-exhausted-fallback-task" / "reports" / "grooming-001.yaml")
        .read_text(encoding="utf-8")
    )
    assert report["verdict"] == "blocked"
    assert report["summary"] == "grooming blocked after exhausting engine fallbacks: quota exceeded"


def test_run_task_pool_stops_by_default_when_limit_fallbacks_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Exhausted fallback task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None):  # type: ignore[no-untyped-def]
        if task.id == "T-0001":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
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
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task_pool(tmp_path)

    assert [execution.task.id for execution in summary.executions if execution.task is not None] == ["T-0001"]
    assert summary.stop_reason == "execution_limit_fallbacks_exhausted"
    state = load_state(tmp_path)
    assert state.queue == ["T-0002"]
    assert state.pool_stop_reason == "execution_limit_fallbacks_exhausted"
    journal = (
        tmp_path / ".litehive" / "tasks" / "T-0001-exhausted-fallback-task" / "journal.md"
    ).read_text(encoding="utf-8")
    assert "Pool stopped: execution_limit_fallbacks_exhausted." in journal
    assert "grooming blocked after exhausting engine fallbacks: quota exceeded" in journal


def test_run_task_pool_rereads_queue_order_between_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)
    third = create_task(tmp_path, title="Third task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None):  # type: ignore[no-untyped-def]
        if task.id == first.id:
            move_queued_task(tmp_path, third.id, 1)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task_pool(tmp_path)

    assert [execution.task.id for execution in summary.executions if execution.task is not None] == [
        first.id,
        third.id,
        second.id,
    ]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_run_task_pool_honors_stop_condition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_task_pool(tmp_path, stop_when=lambda executions: len(executions) >= 1)

    assert [execution.task.id for execution in summary.executions if execution.task is not None] == ["T-0001"]
    assert summary.stop_reason == "stop_condition_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_run_task_pool_restores_preselected_active_task_when_stop_condition_hits(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == first.id
    assert load_state(tmp_path).queue == [second.id]

    summary = run_task_pool(tmp_path, stop_when=lambda executions: True)

    assert summary.executions == []
    assert summary.stop_reason == "stop_condition_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [first.id, second.id]
    assert get_task(tmp_path, first.id).status == "queued"
    assert get_task(tmp_path, second.id).status == "queued"


def test_run_task_pool_stops_after_max_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(max_tasks=1))

    assert [execution.task.id for execution in summary.executions if execution.task is not None] == ["T-0001"]
    assert summary.stop_reason == "max_tasks_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_run_task_pool_stops_on_first_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Failing task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None):  # type: ignore[no-untyped-def]
        if task.id == "T-0001":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
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
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_failure=True))

    assert [execution.task.id for execution in summary.executions if execution.task is not None] == ["T-0001"]
    assert summary.stop_reason == "execution_limit_fallbacks_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_run_task_pool_stops_on_execution_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Limit task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None):  # type: ignore[no-untyped-def]
        if task.id == "T-0001":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="budget exceeded",
                    stderr="",
                ),
                transcript="budget exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="budget limit reached"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_execution_limit=True))

    assert [execution.task.id for execution in summary.executions if execution.task is not None] == ["T-0001"]
    assert summary.stop_reason == "execution_limit_fallbacks_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_run_task_pool_stops_on_quota_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First limit task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second limit task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Third task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None):  # type: ignore[no-untyped-def]
        if task.id in {"T-0001", "T-0002"}:
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
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
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(quota_threshold=2))

    assert [execution.task.id for execution in summary.executions if execution.task is not None] == ["T-0001"]
    assert summary.stop_reason == "execution_limit_fallbacks_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002", "T-0003"]


def test_run_task_pool_stops_on_budget_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Budget task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None):  # type: ignore[no-untyped-def]
        if task.id == "T-0001":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="budget exceeded",
                    stderr="",
                ),
                transcript="budget exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="budget limit reached"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(budget_threshold=1))

    assert [execution.task.id for execution in summary.executions if execution.task is not None] == ["T-0001"]
    assert summary.stop_reason == "execution_limit_fallbacks_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_run_task_pool_stops_on_dirty_git_state(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Queued task", auto_commit=False)
    (tmp_path / "app.txt").write_text("changed\n", encoding="utf-8")

    summary = run_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_dirty_git=True))

    assert summary.executions == []
    assert summary.stop_reason == "dirty_git_state"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0001"]


def test_resolve_next_task_prefers_active_without_mutating_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")

    set_active_task(tmp_path, first.id)
    state_before = load_state(tmp_path)

    task = resolve_next_task(tmp_path)
    state_after = load_state(tmp_path)

    assert task is not None
    assert task.id == first.id
    assert state_before == state_after
    assert second.id in state_after.queue


def test_resolve_next_task_clears_stale_active_and_returns_queued_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Queued task")
    set_active_task(tmp_path, "T-9999")

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == queued.id
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id]


def test_resolve_next_task_skips_ineligible_active_and_queue_entries(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Flagged active task")
    queued = create_task(tmp_path, title="Real queued task")
    completed = create_task(tmp_path, title="Completed queued task")

    active.status = "flagged"
    save_task(tmp_path, active)
    completed.status = "done"
    completed.pipeline_status = "done"
    save_task(tmp_path, completed)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [completed.id, queued.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == queued.id
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id]


def test_resolve_next_task_prefers_ready_prerequisite_over_earlier_blocked_dependent(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked dependent")
    unrelated = create_task(tmp_path, title="Unrelated ready task")
    prerequisite = create_task(tmp_path, title="Ready prerequisite")

    blocked.depends_on = [prerequisite.id]
    save_task(tmp_path, blocked)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == prerequisite.id


def test_peek_next_task_selection_reports_blocked_dependencies(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked dependent")
    prerequisite = create_task(tmp_path, title="Prerequisite")

    blocked.depends_on = [prerequisite.id, "T-9999"]
    save_task(tmp_path, blocked)

    selection = peek_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == prerequisite.id
    assert [entry.task_id for entry in selection.blocked] == [blocked.id]
    assert selection.blocked[0].blocked_by == [f"{prerequisite.id} (queued/backlog)", "T-9999 (missing)"]


def test_run_task_pool_skips_stale_queue_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Real task", auto_commit=False)
    state = load_state(tmp_path)
    state.queue = ["T-9999", task.id]
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, current_task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, current_task.pipeline_status
        ),
    )

    summary = run_task_pool(tmp_path)

    assert [execution.task.id for execution in summary.executions if execution.task is not None] == [task.id]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_run_task_pool_skips_ineligible_active_and_queue_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Flagged active task", auto_commit=False)
    queued = create_task(tmp_path, title="Real task", auto_commit=False)
    completed = create_task(tmp_path, title="Completed queued task", auto_commit=False)

    active.status = "flagged"
    save_task(tmp_path, active)
    completed.status = "done"
    completed.pipeline_status = "done"
    save_task(tmp_path, completed)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [completed.id, queued.id]
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, current_task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, current_task.pipeline_status
        ),
    )

    summary = run_task_pool(tmp_path)

    assert [execution.task.id for execution in summary.executions if execution.task is not None] == [queued.id]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_run_task_pool_reports_blocked_tasks_remaining(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked task", auto_commit=False)
    missing = "T-9999"

    blocked.depends_on = [missing]
    save_task(tmp_path, blocked)

    summary = run_task_pool(tmp_path)

    assert summary.executions == []
    assert summary.stop_reason == "blocked_tasks_remaining"
    assert [entry.task_id for entry in summary.blocked] == [blocked.id]
    assert summary.blocked[0].blocked_by == [f"{missing} (missing)"]
    assert load_state(tmp_path).queue == [blocked.id]


def test_run_task_pool_reports_and_requeues_blocked_active_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked active task", auto_commit=False)
    blocked.depends_on = ["T-9999"]
    save_task(tmp_path, blocked)

    state = load_state(tmp_path)
    state.active_task_id = blocked.id
    state.queue = []
    save_state(tmp_path, state)

    summary = run_task_pool(tmp_path)

    assert summary.executions == []
    assert summary.stop_reason == "blocked_tasks_remaining"
    assert [entry.task_id for entry in summary.blocked] == [blocked.id]
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [blocked.id]


def test_run_task_pool_drains_active_task_without_queued_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resumed task", auto_commit=False)
    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = []
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, current_task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, current_task.pipeline_status
        ),
    )

    summary = run_task_pool(tmp_path)

    assert [execution.task.id for execution in summary.executions if execution.task is not None] == [task.id]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_configure_persists_gemini_model(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="gemini",
        process_profile="generic",
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model="gemini-2.5-pro",
    )

    from litehive.cli import _cmd_configure

    assert _cmd_configure(parser) == 0
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

    from litehive.cli import _cmd_configure

    assert _cmd_configure(parser) == 0
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
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
    )

    from litehive.cli import _cmd_configure

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    context = (tmp_path / ".litehive" / "context.md").read_text(encoding="utf-8")

    assert config.process_profile == "rust"
    assert "Process profile: Rust" in context
    assert "## Init scaffold" in context
    assert "## Rust specifics" in context


def test_configure_persists_pool_stop_defaults(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        pool_stop_on_failure=True,
        pool_max_tasks=2,
        pool_stop_on_limit=True,
        pool_quota_threshold=3,
        pool_budget_threshold=1,
        pool_stop_on_dirty_git=True,
    )

    from litehive.cli import _cmd_configure

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.pool_stop_on_failure is True
    assert config.pool_max_tasks == 2
    assert config.pool_stop_on_execution_limit is True
    assert config.pool_quota_threshold == 3
    assert config.pool_budget_threshold == 1
    assert config.pool_stop_on_dirty_git is True


def test_resolve_engine_name_prefers_run_override_then_task_then_workspace_default(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Queued task", engine="opencode")

    assert resolve_engine_name(task, config, engine_override="gemini") == "gemini"
    assert resolve_engine_name(task, config) == "opencode"

    task.engine = None
    assert resolve_engine_name(task, config) == config.default_engine


def test_cmd_run_dry_run_shows_task_and_engine_without_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued task", engine="opencode")

    def fail_run_task_pool(root: Path) -> None:
        raise AssertionError(f"run_task_pool should not be called for dry-run: {root}")

    monkeypatch.setattr("litehive.cli.run_task_pool", fail_run_task_pool)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 Queued task" in output
    assert "engine: opencode" in output
    assert load_state(tmp_path).queue == ["T-0001"]


def test_cmd_run_dry_run_prefers_run_engine_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued task", engine="opencode")

    def fail_run_task_pool(root: Path, engine_override: str | None = None) -> None:
        raise AssertionError(f"run_task_pool should not be called for dry-run: {root} {engine_override}")

    monkeypatch.setattr("litehive.cli.run_task_pool", fail_run_task_pool)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, engine="gemini"))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 Queued task" in output
    assert "engine: gemini" in output
    assert load_state(tmp_path).queue == ["T-0001"]


def test_run_task_pool_uses_run_engine_override_for_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued task", engine="codex", auto_commit=False)
    seen_engines: list[str] = []

    def fake_run(self, task, role, engine_name, prompt, model=None):  # type: ignore[no-untyped-def]
        seen_engines.append(engine_name)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task_pool(tmp_path, engine_override="opencode")

    assert summary.stop_reason == "queue_exhausted"
    assert seen_engines == ["opencode", "opencode", "opencode", "opencode"]


def test_cmd_run_drains_task_pool_and_reports_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 First task" in output
    assert "task: T-0002 Second task" in output
    assert "tasks_run: 2" in output
    assert "stop_reason: queue_exhausted" in output
    assert load_state(tmp_path).queue == []


def test_cmd_run_reports_blocked_tasks_when_no_runnable_task(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked task", auto_commit=False)
    blocked.depends_on = ["T-9999"]
    save_task(tmp_path, blocked)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No runnable task." in output
    assert f"blocked: {blocked.id} Blocked task blocked_by=T-9999 (missing)" in output
    assert "tasks_run: 0" in output
    assert "stop_reason: blocked_tasks_remaining" in output


def test_cmd_run_reports_pre_execution_stop_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Queued task", auto_commit=False)
    (tmp_path / "app.txt").write_text("changed\n", encoding="utf-8")

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=None,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            stop_on_dirty_git=True,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No task executed." in output
    assert "tasks_run: 0" in output
    assert "stop_reason: dirty_git_state" in output


def test_cmd_run_uses_configured_pool_stop_defaults(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_stop_on_dirty_git=True))
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Queued task", auto_commit=False)
    (tmp_path / "app.txt").write_text("changed\n", encoding="utf-8")

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=None,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            stop_on_dirty_git=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No task executed." in output
    assert "stop_reason: dirty_git_state" in output


def test_status_output_includes_runtime_observability(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_retry_limit=2,
            pool_stop_on_failure=True,
            pool_max_tasks=4,
            pool_stop_on_execution_limit=True,
            pool_quota_threshold=2,
            pool_budget_threshold=1,
            pool_stop_on_dirty_git=True,
        ),
    )
    task = create_task(tmp_path, title="Observe long run")
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    task.retry_policy.max_retries = 1
    task.runtime.execution_status = "running"
    task.runtime.retry_count = 1
    task.runtime.retry_limit = 1
    task.runtime.retry_source = "task"
    task.runtime.run_started_at = "2026-03-31T10:00:00+00:00"
    task.runtime.current_stage = RuntimeStageState(
        step="implementing",
        status="running",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:01+00:00",
        duration_seconds=0,
        summary="",
    )
    task.runtime.last_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="completed",
        path="subagents/SA-0001-swe",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:02:00+00:00",
        completed_at="2026-03-31T10:02:00+00:00",
        exit_code=0,
        transcript_snippet="implemented live observability",
    )
    task.runtime.last_stage = RuntimeStageState(
        step="grooming",
        status="completed",
        started_at="2026-03-31T09:59:00+00:00",
        completed_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:00+00:00",
        duration_seconds=60,
        verdict="pass",
        summary="plan confirmed",
    )
    task.runtime.last_outcome.kind = "blocked"
    task.runtime.last_outcome.stage = "testing"
    task.runtime.last_outcome.reason = "waiting on fixture update"
    task.runtime.last_outcome.recorded_at = "2026-03-31T10:02:30+00:00"

    save_task(tmp_path, task)

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "default_retry_limit: 2" in output
    assert "pool_stop_on_failure: True" in output
    assert "pool_max_tasks: 4" in output
    assert "pool_stop_on_execution_limit: True" in output
    assert "pool_quota_threshold: 2" in output
    assert "pool_budget_threshold: 1" in output
    assert "pool_stop_on_dirty_git: True" in output
    assert "pool_stop_reason: None" in output
    assert "process_profile: generic" in output
    assert "retry_limit=1" in output
    assert "retry_policy=configured:1 effective:1 source=task" in output
    assert "run=running" in output
    assert "retries=1/1" in output
    assert "retry_source=task" in output
    assert "stage=implementing" in output
    assert "last_subagent=SA-0001 swe/codex completed snippet=implemented live observability" in output
    assert "last_report=grooming/pass duration=1m00s summary=plan confirmed" in output
    assert "outcome=blocked stage=testing recorded_at=2026-03-31T10:02:30+00:00 reason=waiting on fixture update" in output


def test_queue_command_shows_active_and_queued_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task", engine="opencode")
    second.depends_on = [first.id]
    save_task(tmp_path, second)

    set_active_task(tmp_path, first.id)

    exit_code = _cmd_queue(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"active_task_id: {first.id}" in output
    assert (
        f"active: {first.id} [queued/backlog] priority=medium engine=codex (default) "
        "title=First task depends_on=-"
    ) in output
    assert (
        f"1. {second.id} [queued/backlog] priority=medium engine=opencode "
        f"title=Second task depends_on={first.id}"
    ) in output


def test_add_command_persists_dependencies(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First prerequisite")
    second = create_task(tmp_path, title="Second prerequisite")

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Dependent task",
            goal="",
            depends_on=[first.id, f"{second.id},{first.id}"],
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0003")
    assert task is not None
    assert task.depends_on == [first.id, second.id]
    assert f"depends_on: {first.id}, {second.id}" in output


def test_update_command_replaces_and_clears_dependencies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First prerequisite")
    second = create_task(tmp_path, title="Second prerequisite")
    task = create_task(tmp_path, title="Dependent task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=[first.id, f"{second.id},{first.id}"],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.depends_on == [first.id, second.id]
    assert f"depends_on: {first.id}, {second.id}" in output

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=["none"],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    cleared = get_task(tmp_path, task.id)
    assert cleared is not None
    assert cleared.depends_on == []
    assert "depends_on: -" in output


def test_move_command_reorders_queue(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    third = create_task(tmp_path, title="Third task")

    exit_code = _cmd_move(argparse.Namespace(workspace=tmp_path, task_id=third.id, position=1))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "position: 1" in output
    assert load_state(tmp_path).queue == [third.id, first.id, second.id]


def test_promote_command_moves_queued_task_to_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")

    exit_code = _cmd_promote(argparse.Namespace(workspace=tmp_path, task_id=second.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "position: 1" in output
    assert load_state(tmp_path).queue == [second.id, first.id]


def test_requeue_command_requeues_flagged_task_to_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Needs another pass")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    from litehive.tasks import save_task

    save_task(tmp_path, task)

    exit_code = _cmd_requeue_task(argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: implementing" in output
    assert load_state(tmp_path).queue == [flagged.id, first.id]
    requeued = get_task(tmp_path, flagged.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.pipeline_status == "implementing"


def test_requeue_command_requires_flagged_or_cancelled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Still queued")

    exit_code = _cmd_requeue_task(argparse.Namespace(workspace=tmp_path, task_id=task.id, front=False))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "is not flagged, failed, or cancelled" in output


def test_update_command_updates_task_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="opencode",
            retry_limit="2",
            priority="high",
            goal="Ship queue CLI",
            mode="tasks",
            auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "opencode"
    assert updated.retry_policy.max_retries == 2
    assert updated.priority == "high"
    assert updated.goal == "Ship queue CLI"
    assert updated.mode == "tasks"
    assert updated.git.auto_commit is False
    assert "engine: opencode" in output
    assert "retry_limit: 2" in output
    assert "priority: high" in output


def test_update_command_clears_task_retry_override(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune retry policy", retry_limit=2)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine=None,
            retry_limit="default",
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "retry_limit: default" in output
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.retry_policy.max_retries is None


def test_update_command_accepts_gemini_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune Gemini task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="gemini",
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "gemini"
    assert "engine: gemini" in output


def test_update_command_accepts_copilot_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune Copilot task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="copilot",
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "copilot"
    assert "engine: copilot" in output


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _init_git_repo(tmp_path: Path) -> str:
    _run(["git", "init"], tmp_path)
    _run(["git", "config", "user.name", "Litehive Tests"], tmp_path)
    _run(["git", "config", "user.email", "tests@example.com"], tmp_path)
    (tmp_path / "app.txt").write_text("base\n", encoding="utf-8")
    _run(["git", "add", "app.txt"], tmp_path)
    _run(["git", "commit", "-m", "initial"], tmp_path)
    return _run(["git", "rev-parse", "HEAD"], tmp_path)


def _completed_subagent_result(tmp_path: Path, step: str) -> SubagentResult:
    return SubagentResult(
        ref=SubagentRef(
            id=f"SA-{step}",
            role="swe",
            engine="codex",
            status="completed",
            path=f"subagents/{step}",
        ),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=tmp_path,
            exit_code=0,
            stdout=(
                "VERDICT: PASS\n"
                f"SUMMARY: {step} complete\n"
                "FILES_CHANGED:\n"
                "- app.txt\n"
                "TESTS_ADDED: 1\n"
                "TESTS_PASSING: 1\n"
                "WARNINGS:\n"
            ),
            stderr="",
        ),
        transcript="",
        exit_code=0,
    )


def _successful_stage_execution(tmp_path: Path, adapter: str, step: str) -> CLIExecutionResult:
    return CLIExecutionResult(
        adapter=adapter,
        argv=(adapter, "exec"),
        cwd=tmp_path,
        exit_code=0,
        stdout=(
            "VERDICT: PASS\n"
            f"SUMMARY: {step} complete via {adapter}\n"
            "FILES_CHANGED:\n"
            "- app.txt\n"
            "TESTS_ADDED: 1\n"
            "TESTS_PASSING: 1\n"
            "WARNINGS:\n"
        ),
        stderr="",
    )


def test_classify_execution_limit_matches_codex_usage_limit_transcript() -> None:
    transcript = (
        "[stderr]\n"
        "ERROR: You've hit your usage limit. Upgrade to Pro "
        "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits or try again at 5:26 PM."
    )

    assert classify_execution_limit(transcript) == "usage limit reached"


def test_run_next_task_falls_back_from_codex_to_opencode_on_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fallback usage-limit task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    opencode = get_engine("opencode")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(opencode, "is_available", lambda: True)

    def fake_codex_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
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

    def fake_opencode_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
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
    assert "Stage `grooming` switched from `codex` to `opencode` after usage limit reached." in report["warnings"]
    assert report["feedback"].startswith("Stage `grooming` switched from `codex` to `opencode` after usage limit reached.")
    assert "SUMMARY: grooming complete via opencode" in report["feedback"]
    _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out
    assert "engine_switch=grooming codex->opencode reason=usage limit reached" in output


def test_run_next_task_falls_back_from_codex_to_gemini_on_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_fallbacks={
                "codex": ["gemini"],
                "opencode": ["codex", "gemini", "copilot"],
                "gemini": ["codex", "opencode", "copilot"],
                "copilot": ["codex", "opencode", "gemini"],
            }
        ),
    )
    create_task(tmp_path, title="Gemini fallback task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    gemini = get_engine("gemini")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(gemini, "is_available", lambda: True)

    def fake_codex_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
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

    def fake_gemini_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        transcript = (
            '{"type":"message","role":"assistant","content":"VERDICT: PASS\\n","delta":true}\n'
            f'{{"type":"message","role":"assistant","content":"SUMMARY: {step} complete via gemini\\nFILES_CHANGED:\\n- app.txt\\nTESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n","delta":true}}'
        )
        return CLIExecutionResult(
            adapter="gemini",
            argv=("gemini", "-p"),
            cwd=cwd,
            exit_code=0,
            stdout=transcript,
            stderr="",
        )

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
    assert "Stage `grooming` switched from `codex` to `gemini` after usage limit reached." in report["warnings"]


def test_run_next_task_skips_unavailable_fallback_engine_after_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_fallbacks={
                "codex": ["gemini", "opencode"],
                "opencode": ["codex", "gemini", "copilot"],
                "gemini": ["codex", "opencode", "copilot"],
                "copilot": ["codex", "opencode", "gemini"],
            }
        ),
    )
    create_task(tmp_path, title="Unavailable fallback task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    gemini = get_engine("gemini")
    opencode = get_engine("opencode")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(gemini, "is_available", lambda: False)
    monkeypatch.setattr(opencode, "is_available", lambda: True)

    def fake_codex_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
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

    def fake_opencode_run(prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
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
    assert "Stage `grooming` switched from `codex` to `gemini` after usage limit reached." in report["warnings"]
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
        lambda self, task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha is not None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "litehive: checkpoint T-0001 ship-checkpoint"

    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.commit_sha == summary.commit_sha
    assert task.git.checkpoint_attempts == 1
    assert task.git.checkpoint_base_sha == initial_sha
    assert task.git.rolled_back_checkpoint_attempt is None
    assert task.runtime.execution_status == "done"
    assert task.runtime.last_stage.step == "commit_to_git"
    assert task.runtime.last_stage.verdict == "pass"


def test_run_next_task_flags_task_when_commit_stage_prerequisite_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Needs git repo")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "flagged"
    assert task.pipeline_status == "commit_to_git"
    assert task.git.commit_sha is None


def test_run_next_task_skips_commit_stage_when_auto_commit_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Skip commit", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
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
        lambda self, task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    assert summary.commit_sha is None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "initial"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "flagged"
    assert task.pipeline_status == "commit_to_git"
    assert task.git.commit_sha is None


def test_run_next_task_flags_task_when_other_task_state_is_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="Ship first task")
    create_task(tmp_path, title="Unrelated queued task")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.task.id == first.id
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    assert summary.commit_sha is None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "initial"
    task = get_task(tmp_path, first.id)
    assert task is not None
    assert task.status == "flagged"
    assert task.pipeline_status == "commit_to_git"
    assert task.git.commit_sha is None


def test_rollback_command_requeues_checkpointed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fix after done")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)

    exit_code = _cmd_rollback(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    rollback_output = capsys.readouterr().out

    assert exit_code == 0
    assert "rollback_commit:" in rollback_output
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "litehive: rollback T-0001 fix-after-done (attempt 1)"
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
        lambda self, task, role, engine_name, prompt, model=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    recover_output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 Recover without revert" in recover_output
    assert "pipeline_status: implementing" in recover_output
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "ship-again\n"
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
