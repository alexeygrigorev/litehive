"""Tests for crash resume and timeout verdict nudges across engines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litehive.agents._models import EngineFailure
from litehive.config import ExecutionRetryPolicy, load_config
from litehive.models import StageReport
from litehive.pipeline._builder import build_executor
from litehive.pipeline._types import ResolvedExecutionRetryPolicy
from litehive.runtime import EngineBudgetLedger, SubagentManager
from tests.workspace_helpers import (
    CLIExecutionResult,
    LitehiveConfig,
    SubagentRef,
    SubagentResult,
    _completed_subagent_result,
    create_task,
    ensure_workspace,
    require_task,
)


def _claude_init_stdout(session_id: str) -> str:
    """Build JSONL stdout containing a claude session init payload."""
    return json.dumps({"type": "system", "subtype": "init", "session_id": session_id}) + "\n"


def _codex_thread_started_stdout(thread_id: str) -> str:
    """Build JSONL stdout containing a codex thread-start payload."""
    return json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n"


def _execution_stdout(engine_name: str, resume_id: str | None) -> str:
    if engine_name == "claude" and resume_id:
        return _claude_init_stdout(resume_id)
    if engine_name == "codex" and resume_id:
        return _codex_thread_started_stdout(resume_id)
    return ""


def _crash_result(
    step: str = "implementing",
    *,
    engine_name: str,
    resume_id: str | None = None,
) -> SubagentResult:
    """Return a SubagentResult that simulates an unclassified crash (exit 1, no failure)."""
    return SubagentResult(
        ref=SubagentRef(
            id=f"SA-{step}-crash",
            role="swe",
            engine=engine_name,
            status="failed",
            path=f"subagents/{step}",
        ),
        execution=CLIExecutionResult(
            adapter=engine_name,
            argv=(engine_name, "exec"),
            cwd=Path("/tmp"),
            exit_code=1,
            stdout=_execution_stdout(engine_name, resume_id),
            stderr="",
        ),
        transcript="",
        exit_code=1,
        failure=None,
    )


def _timeout_result(
    *,
    engine_name: str,
    resume_id: str,
    transcript: str = "initial timeout transcript",
) -> SubagentResult:
    return SubagentResult(
        ref=SubagentRef(
            id="SA-implementing-timeout",
            role="swe",
            engine=engine_name,
            status="failed",
            path="subagents/implementing-timeout",
        ),
        execution=CLIExecutionResult(
            adapter=engine_name,
            argv=(engine_name, "exec"),
            cwd=Path("/tmp"),
            exit_code=124,
            stdout=_execution_stdout(engine_name, resume_id),
            stderr="timed out",
        ),
        transcript=transcript,
        exit_code=124,
        failure=EngineFailure(
            kind="retryable_execution_error",
            reason="transient timeout",
            classification="timeout",
        ),
    )


def _build_stage_executor(
    tmp_path: Path, task, engine_name: str, monkeypatch: pytest.MonkeyPatch
):
    config = load_config(tmp_path)
    monkeypatch.setattr("litehive.pipeline._builder.check_codex_quota", lambda: None)
    monkeypatch.setattr("litehive.pipeline._builder.codex_quota_block_reason", lambda: None)
    monkeypatch.setattr("litehive.pipeline._builder.claude_quota_block_reason", lambda: None)
    monkeypatch.setattr("litehive.pipeline._builder.copilot_quota_block_reason", lambda: None)
    monkeypatch.setattr("litehive.pipeline._builder.zai_quota_block_reason", lambda: None)
    return build_executor(
        tmp_path,
        execution_root=tmp_path,
        initial_engine_names=[engine_name],
        workspace_context="",
        subagents=SubagentManager(tmp_path),
        config=config,
        task=task,
        model_override=None,
        config_auto_commit=False,
        budget_ledger=EngineBudgetLedger(),
    )


def _disable_execution_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "litehive.pipeline._builder.resolve_execution_retry_policy",
        lambda config, engine_name, model_name: ResolvedExecutionRetryPolicy(
            selector=engine_name,
            policy=ExecutionRetryPolicy(max_retries=0, retry_on=[]),
        ),
    )


def test_crash_resume_triggers_session_resume_for_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Crash resume task", engine="claude", auto_commit=False)
    task = require_task(tmp_path, "T-0001")
    executor = _build_stage_executor(tmp_path, task, "claude", monkeypatch)

    calls: list[dict[str, str | None]] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        calls.append({"prompt": prompt, "resume_session_id": resume_session_id})
        if len(calls) == 1:
            return _crash_result(engine_name="claude", resume_id="ses-abc-789")
        return _completed_subagent_result(tmp_path, "implementing", engine_name=engine_name, task=task_arg)

    monkeypatch.setattr("litehive.pipeline._builder.SubagentManager.run", fake_run)

    report = executor(task, "implementing")

    assert report.verdict == "pass"
    assert len(calls) == 2
    assert calls[1]["resume_session_id"] == "ses-abc-789"
    assert "continue where you left off" in (calls[1]["prompt"] or "")


def test_crash_resume_triggers_session_resume_for_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Codex crash resume task", engine="codex", auto_commit=False)
    task = require_task(tmp_path, "T-0001")
    executor = _build_stage_executor(tmp_path, task, "codex", monkeypatch)

    calls: list[dict[str, str | None]] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        calls.append({"prompt": prompt, "resume_session_id": resume_session_id})
        if len(calls) == 1:
            return _crash_result(engine_name="codex", resume_id="thread-abc-789")
        return _completed_subagent_result(tmp_path, "implementing", engine_name=engine_name, task=task_arg)

    monkeypatch.setattr("litehive.pipeline._builder.SubagentManager.run", fake_run)

    report = executor(task, "implementing")

    assert report.verdict == "pass"
    assert len(calls) == 2
    assert calls[1]["resume_session_id"] == "thread-abc-789"
    assert "continue where you left off" in (calls[1]["prompt"] or "")


@pytest.mark.parametrize("engine_name", ["copilot", "goz"])
def test_crash_resume_skips_engines_without_resume_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_name: str
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine=engine_name))
    create_task(tmp_path, title=f"{engine_name} crash task", engine=engine_name, auto_commit=False)
    task = require_task(tmp_path, "T-0001")
    executor = _build_stage_executor(tmp_path, task, engine_name, monkeypatch)

    calls: list[dict[str, str | None]] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        del self, task_arg, role, prompt, model, max_turns
        calls.append({"resume_session_id": resume_session_id})
        return _crash_result(engine_name=engine_name, resume_id=None)

    monkeypatch.setattr("litehive.pipeline._builder.SubagentManager.run", fake_run)

    report = executor(task, "implementing")

    assert report.verdict == "fail"
    assert len(calls) == 1
    assert calls[0]["resume_session_id"] is None


def test_verdict_nudge_fires_on_timeout_when_resume_id_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Timeout nudge task", engine="claude", auto_commit=False)
    task = require_task(tmp_path, "T-0001")
    _disable_execution_retries(monkeypatch)
    executor = _build_stage_executor(tmp_path, task, "claude", monkeypatch)

    calls: list[dict[str, str | None]] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        calls.append({"prompt": prompt, "resume_session_id": resume_session_id})
        if len(calls) == 1:
            return _timeout_result(engine_name="claude", resume_id="ses-timeout-123")
        return _completed_subagent_result(tmp_path, "implementing", engine_name=engine_name, task=task_arg)

    monkeypatch.setattr("litehive.pipeline._builder.SubagentManager.run", fake_run)

    report = executor(task, "implementing")

    assert report.verdict == "pass"
    assert len(calls) == 2
    assert calls[1]["resume_session_id"] == "ses-timeout-123"
    assert "You did not submit your verdict" in (calls[1]["prompt"] or "")


def test_timeout_nudge_result_replaces_original_result_for_report_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Codex timeout nudge task", engine="codex", auto_commit=False)
    task = require_task(tmp_path, "T-0001")
    _disable_execution_retries(monkeypatch)
    executor = _build_stage_executor(tmp_path, task, "codex", monkeypatch)

    timeout_result = _timeout_result(
        engine_name="codex",
        resume_id="thread-timeout-123",
        transcript="original timeout transcript",
    )
    nudged_result = SubagentResult(
        ref=SubagentRef(
            id="SA-implementing-nudge",
            role="swe",
            engine="codex",
            status="completed",
            path="subagents/implementing-nudge",
        ),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=Path("/tmp"),
            exit_code=0,
            stdout=_codex_thread_started_stdout("thread-timeout-123"),
            stderr="",
        ),
        transcript="nudged transcript",
        exit_code=0,
    )
    calls: list[dict[str, str | None]] = []
    seen_results: list[SubagentResult] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        del task_arg, role, engine_name, model, max_turns
        calls.append({"prompt": prompt, "resume_session_id": resume_session_id})
        return timeout_result if len(calls) == 1 else nudged_result

    def fake_stage_report(current_task, step, result, *, root=None):  # type: ignore[no-untyped-def]
        del current_task, step, root
        seen_results.append(result)
        return StageReport(
            task_id=task.id,
            step="implementing",
            verdict="pass",
            summary=result.transcript,
            files_changed=["app.txt"],
            tests={"added": 1, "passing": 1},
        )

    monkeypatch.setattr("litehive.pipeline._builder.SubagentManager.run", fake_run)
    monkeypatch.setattr("litehive.pipeline._builder.stage_report_from_subagent", fake_stage_report)

    report = executor(task, "implementing")

    assert len(calls) == 2
    assert calls[1]["resume_session_id"] == "thread-timeout-123"
    assert seen_results == [nudged_result]
    assert report.summary == "nudged transcript"
