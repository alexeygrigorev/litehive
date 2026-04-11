"""Tests for crash resume and timeout verdict nudges across engines."""

import json
from pathlib import Path

import pytest

from litehive.agents._models import EngineFailure
from litehive.config import ExecutionRetryPolicy, load_config
from litehive.models import StageReport
from litehive.pipeline_old import EngineBudgetLedger, SubagentManager
from litehive.pipeline_old._builder import build_executor
from litehive.pipeline_old._types import ResolvedExecutionRetryPolicy
from tests.workspace_helpers import (
    CLIExecutionResult,
    LitehiveConfig,
    SubagentRef,
    SubagentResult,
    _completed_subagent_result,
    create_task,
    ensure_workspace,
    require_task,
    run_task,
    save_task,
)


def _continuation_stdout(engine_name: str, session_id: str) -> str:
    """Build JSONL stdout containing the engine-specific continuation payload."""
    if engine_name == "claude":
        payload = {"type": "system", "subtype": "init", "session_id": session_id}
    elif engine_name == "codex":
        payload = {"type": "thread.started", "thread_id": session_id}
    elif engine_name == "gemini":
        payload = {"type": "init", "session_id": session_id}
    elif engine_name == "opencode":
        payload = {"type": "step_start", "sessionID": session_id}
    else:
        raise ValueError(f"Unsupported engine for continuation fixture: {engine_name}")
    return json.dumps(payload) + "\n"


def _disable_quota_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("litehive.pipeline_old._models._engine_quota_block", lambda *args, **kwargs: (None, None))


def _set_task_to_implementing(tmp_path: Path):
    task = require_task(tmp_path, "T-0001")
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)
    return task


def _codex_thread_started_stdout(thread_id: str) -> str:
    """Build JSONL stdout containing a codex thread-start payload."""
    return json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n"


def _execution_stdout(engine_name: str, resume_id: str | None) -> str:
    if resume_id is None:
        return ""
    return _continuation_stdout(engine_name, resume_id)


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
    _disable_quota_gates(monkeypatch)
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
        "litehive.pipeline_old._builder.resolve_execution_retry_policy",
        lambda config, engine_name, model_name: ResolvedExecutionRetryPolicy(
            selector=engine_name,
            policy=ExecutionRetryPolicy(max_retries=0, retry_on=[]),
        ),
    )


def test_run_task_crash_resume_triggers_session_resume_for_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unclassified crash with exit 1 on claude triggers one resume attempt with session ID."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Crash resume task", auto_commit=False)
    _disable_quota_gates(monkeypatch)
    task = _set_task_to_implementing(tmp_path)

    calls: list[dict] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        calls.append(
            {
                "engine": engine_name,
                "resume_session_id": resume_session_id,
                "prompt": prompt,
            }
        )
        if len(calls) == 1:
            return _crash_result("implementing", engine_name="claude", resume_id="ses-abc-789")
        return _completed_subagent_result(tmp_path, task_arg.pipeline_status, task=task_arg)

    monkeypatch.setattr("litehive.pipeline_old._orchestration.SubagentManager.run", fake_run)
    summary = run_task(tmp_path, task)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    resume_calls = [c for c in calls if c["prompt"] and "continue where you left off" in c["prompt"]]
    assert len(resume_calls) >= 1, f"Expected at least one resume call, got calls: {calls}"
    assert resume_calls[0]["resume_session_id"] == "ses-abc-789"

    task_folder = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}"
    journal = (task_folder / "journal.md").read_text(encoding="utf-8")
    assert "resuming claude session" in journal
    assert "ses-abc-789" in journal


def test_run_task_crash_resume_only_once_per_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the resumed session also crashes, do not attempt another resume."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Double crash task", auto_commit=False)
    _disable_quota_gates(monkeypatch)
    task = _set_task_to_implementing(tmp_path)

    calls: list[dict] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        calls.append(
            {
                "engine": engine_name,
                "resume_session_id": resume_session_id,
                "prompt": prompt,
                "step": task_arg.pipeline_status,
            }
        )
        if task_arg.pipeline_status == "implementing":
            return _crash_result("implementing", engine_name="claude", resume_id="ses-double-crash")
        return _completed_subagent_result(tmp_path, task_arg.pipeline_status, task=task_arg)

    monkeypatch.setattr("litehive.pipeline_old._orchestration.SubagentManager.run", fake_run)
    run_task(tmp_path, task)

    resume_calls = [
        c
        for c in calls
        if c["resume_session_id"] == "ses-double-crash"
        and "continue where you left off" in c["prompt"]
    ]
    assert len(resume_calls) == 1, (
        f"Expected exactly 1 crash-resume call, got {len(resume_calls)}: {resume_calls}"
    )


@pytest.mark.parametrize(
    ("engine_name", "resume_id"),
    [
        ("codex", "thread-codex-123"),
        ("gemini", "session-gemini-123"),
        ("opencode", "session-opencode-123"),
    ],
)
def test_run_task_crash_resume_triggers_for_engines_with_resume_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_name: str, resume_id: str
) -> None:
    """Unclassified crashes resume once for any engine that exposes a continuation ID."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine=engine_name))
    create_task(tmp_path, title=f"{engine_name} crash task", auto_commit=False)
    _disable_quota_gates(monkeypatch)
    task = _set_task_to_implementing(tmp_path)

    calls: list[dict] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        calls.append(
            {
                "engine": engine_name,
                "resume_session_id": resume_session_id,
                "prompt": prompt,
                "step": task_arg.pipeline_status,
            }
        )
        if len(calls) == 1:
            return _crash_result("implementing", engine_name=engine_name, resume_id=resume_id)
        return _completed_subagent_result(tmp_path, task_arg.pipeline_status, task=task_arg)

    monkeypatch.setattr("litehive.pipeline_old._orchestration.SubagentManager.run", fake_run)
    summary = run_task(tmp_path, task)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    resume_calls = [
        c
        for c in calls
        if c.get("resume_session_id") == resume_id
        and "continue where you left off" in (c.get("prompt") or "")
    ]
    assert len(resume_calls) == 1, (
        f"Expected exactly 1 resume call for {engine_name}, got {len(resume_calls)}: {resume_calls}"
    )


def test_run_task_crash_resume_skipped_when_no_resume_id_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unclassified crashes without a continuation ID should not trigger resume."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Codex crash task", auto_commit=False)
    _disable_quota_gates(monkeypatch)
    task = _set_task_to_implementing(tmp_path)

    calls: list[dict] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        calls.append(
            {
                "engine": engine_name,
                "resume_session_id": resume_session_id,
                "prompt": prompt,
                "step": task_arg.pipeline_status,
            }
        )
        if task_arg.pipeline_status == "implementing" and len(
            [c for c in calls if c["step"] == "implementing"]
        ) == 1:
            return SubagentResult(
                ref=SubagentRef(
                    id="SA-impl-crash",
                    role="swe",
                    engine="codex",
                    status="failed",
                    path="subagents/implementing",
                ),
                execution=CLIExecutionResult(
                    adapter="codex",
                    argv=("codex", "exec"),
                    cwd=Path("/tmp"),
                    exit_code=1,
                    stdout="",
                    stderr="",
                ),
                transcript="",
                exit_code=1,
                failure=None,
            )
        return _completed_subagent_result(tmp_path, task_arg.pipeline_status, task=task_arg)

    monkeypatch.setattr("litehive.pipeline_old._orchestration.SubagentManager.run", fake_run)
    run_task(tmp_path, task)

    resume_calls = [
        c
        for c in calls
        if c.get("resume_session_id") is not None
        and "continue where you left off" in (c.get("prompt") or "")
    ]
    assert len(resume_calls) == 0, (
        f"Expected 0 resume calls for codex, got {len(resume_calls)}: {resume_calls}"
    )


def test_run_task_crash_resume_journal_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Journal clearly logs the resume attempt with session ID and exit code."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Journal crash task", auto_commit=False)
    _disable_quota_gates(monkeypatch)
    task = _set_task_to_implementing(tmp_path)

    impl_call_count = 0

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        nonlocal impl_call_count
        if task_arg.pipeline_status == "implementing":
            impl_call_count += 1
            if impl_call_count == 1:
                return _crash_result("implementing", engine_name="claude", resume_id="ses-journal-456")
        return _completed_subagent_result(tmp_path, task_arg.pipeline_status, task=task_arg)

    monkeypatch.setattr("litehive.pipeline_old._orchestration.SubagentManager.run", fake_run)
    run_task(tmp_path, task)

    task_folder = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}"
    journal = (task_folder / "journal.md").read_text(encoding="utf-8")

    assert "crashed" in journal.lower() or "crash" in journal.lower()
    assert "ses-journal-456" in journal
    assert "resuming" in journal.lower()


def test_stage_executor_crash_resume_triggers_session_resume_for_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Crash resume task", auto_commit=False)
    task = require_task(tmp_path, "T-0001")
    executor = _build_stage_executor(tmp_path, task, "claude", monkeypatch)

    calls: list[dict[str, str | None]] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        calls.append({"prompt": prompt, "resume_session_id": resume_session_id})
        if len(calls) == 1:
            return _crash_result(engine_name="claude", resume_id="ses-abc-789")
        return _completed_subagent_result(
            tmp_path, "implementing", engine_name=engine_name, task=task_arg
        )

    monkeypatch.setattr("litehive.pipeline_old._builder.SubagentManager.run", fake_run)

    report = executor(task, "implementing")

    assert report.verdict == "pass"
    assert len(calls) == 2
    assert calls[1]["resume_session_id"] == "ses-abc-789"
    assert "continue where you left off" in (calls[1]["prompt"] or "")


def test_stage_executor_crash_resume_triggers_session_resume_for_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Codex crash resume task", auto_commit=False)
    task = require_task(tmp_path, "T-0001")
    executor = _build_stage_executor(tmp_path, task, "codex", monkeypatch)

    calls: list[dict[str, str | None]] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        calls.append({"prompt": prompt, "resume_session_id": resume_session_id})
        if len(calls) == 1:
            return _crash_result(engine_name="codex", resume_id="thread-abc-789")
        return _completed_subagent_result(
            tmp_path, "implementing", engine_name=engine_name, task=task_arg
        )

    monkeypatch.setattr("litehive.pipeline_old._builder.SubagentManager.run", fake_run)

    report = executor(task, "implementing")

    assert report.verdict == "pass"
    assert len(calls) == 2
    assert calls[1]["resume_session_id"] == "thread-abc-789"
    assert "continue where you left off" in (calls[1]["prompt"] or "")


@pytest.mark.parametrize("engine_name", ["copilot", "goz"])
def test_stage_executor_crash_resume_skips_engines_without_resume_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_name: str
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine=engine_name))
    create_task(tmp_path, title=f"{engine_name} crash task", auto_commit=False)
    task = require_task(tmp_path, "T-0001")
    executor = _build_stage_executor(tmp_path, task, engine_name, monkeypatch)

    calls: list[dict[str, str | None]] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        del self, task_arg, role, prompt, model, max_turns
        calls.append({"resume_session_id": resume_session_id})
        return _crash_result(engine_name=engine_name, resume_id=None)

    monkeypatch.setattr("litehive.pipeline_old._builder.SubagentManager.run", fake_run)

    report = executor(task, "implementing")

    assert report.verdict == "reject"
    assert len(calls) == 1
    assert calls[0]["resume_session_id"] is None


def test_verdict_nudge_fires_on_timeout_when_resume_id_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Timeout nudge task", auto_commit=False)
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
        return _completed_subagent_result(
            tmp_path, "implementing", engine_name=engine_name, task=task_arg
        )

    monkeypatch.setattr("litehive.pipeline_old._builder.SubagentManager.run", fake_run)

    report = executor(task, "implementing")

    assert report.verdict == "pass"
    assert len(calls) == 2
    assert calls[1]["resume_session_id"] == "ses-timeout-123"
    assert "You did not submit your verdict" in (calls[1]["prompt"] or "")


def test_verdict_nudge_fires_on_clean_exit_without_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Clean exit nudge task", auto_commit=False)
    task = require_task(tmp_path, "T-0001")
    executor = _build_stage_executor(tmp_path, task, "codex", monkeypatch)

    initial_result = SubagentResult(
        ref=SubagentRef(
            id="SA-implementing-clean-exit",
            role="swe",
            engine="codex",
            status="completed",
            path="subagents/implementing-clean-exit",
        ),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=Path("/tmp"),
            exit_code=0,
            stdout=_codex_thread_started_stdout("thread-clean-123"),
            stderr="",
        ),
        transcript="finished work but no verdict",
        exit_code=0,
    )
    nudged_result = _completed_subagent_result(
        tmp_path, "implementing", engine_name="codex", task=task
    )
    calls: list[dict[str, str | None]] = []
    seen_results: list[SubagentResult] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        del self, task_arg, role, engine_name, model, max_turns
        calls.append({"prompt": prompt, "resume_session_id": resume_session_id})
        return initial_result if len(calls) == 1 else nudged_result

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

    monkeypatch.setattr("litehive.pipeline_old._builder.SubagentManager.run", fake_run)
    monkeypatch.setattr("litehive.pipeline_old._builder.stage_report_from_subagent", fake_stage_report)

    report = executor(task, "implementing")

    assert report.verdict == "pass"
    assert len(calls) == 2
    assert calls[0]["resume_session_id"] is None
    assert calls[1]["resume_session_id"] == "thread-clean-123"
    assert "You did not submit your verdict" in (calls[1]["prompt"] or "")
    assert seen_results == [nudged_result]
    assert report.summary == nudged_result.transcript


def test_timeout_nudge_result_replaces_original_result_for_report_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Codex timeout nudge task", auto_commit=False)
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

    monkeypatch.setattr("litehive.pipeline_old._builder.SubagentManager.run", fake_run)
    monkeypatch.setattr("litehive.pipeline_old._builder.stage_report_from_subagent", fake_stage_report)

    report = executor(task, "implementing")

    assert len(calls) == 2
    assert calls[1]["resume_session_id"] == "thread-timeout-123"
    assert seen_results == [nudged_result]
    assert report.summary == "nudged transcript"
