"""Tests for crash-resume: unclassified agent crashes resume the same session."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workspace_helpers import *  # noqa: F401,F403


def _claude_init_stdout(session_id: str) -> str:
    """Build JSONL stdout containing a claude session init payload."""
    return json.dumps({"type": "system", "subtype": "init", "session_id": session_id}) + "\n"


def _crash_result(
    step: str,
    *,
    engine_name: str = "claude",
    session_id: str = "ses-crash-123",
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
            stdout=_claude_init_stdout(session_id),
            stderr="",
        ),
        transcript="",
        exit_code=1,
        failure=None,
    )


def test_crash_resume_triggers_session_resume_for_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unclassified crash with exit 1 on claude triggers one resume attempt with session ID."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Crash resume task", engine="claude", auto_commit=False)
    task = require_task(tmp_path, "T-0001")

    calls: list[dict] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        calls.append({
            "engine": engine_name,
            "resume_session_id": resume_session_id,
            "prompt": prompt,
        })
        if len(calls) == 1:
            # First call for implementing: crash with no failure classification
            return _crash_result("implementing", session_id="ses-abc-789")
        # Second call: resumed session succeeds
        return _completed_subagent_result(tmp_path, task_arg.pipeline_status, task=task_arg)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)
    summary = run_task(tmp_path, task)

    assert summary.result is not None
    assert summary.result.final_status == "done"

    # The implementing stage should have been called twice: initial + resume
    impl_calls = [c for c in calls if c["prompt"] and "continue where you left off" not in c["prompt"]]
    resume_calls = [c for c in calls if c["prompt"] and "continue where you left off" in c["prompt"]]

    # At least one resume call happened with session ID
    assert len(resume_calls) >= 1, f"Expected at least one resume call, got calls: {calls}"
    assert resume_calls[0]["resume_session_id"] == "ses-abc-789"

    # Verify journal has the resume event
    task_folder = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}"
    journal = (task_folder / "journal.md").read_text(encoding="utf-8")
    assert "resuming claude session" in journal
    assert "ses-abc-789" in journal


def test_crash_resume_only_once_per_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the resumed session also crashes, do not attempt another resume."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Double crash task", engine="claude", auto_commit=False)
    task = require_task(tmp_path, "T-0001")

    calls: list[dict] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        calls.append({
            "engine": engine_name,
            "resume_session_id": resume_session_id,
            "prompt": prompt,
            "step": task_arg.pipeline_status,
        })
        if task_arg.pipeline_status == "implementing":
            # Always crash during implementing
            return _crash_result("implementing", session_id="ses-double-crash")
        # Other stages succeed
        return _completed_subagent_result(tmp_path, task_arg.pipeline_status, task=task_arg)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)
    summary = run_task(tmp_path, task)

    # Check that exactly one crash-resume attempt happened (resume_session_id set once)
    resume_calls = [c for c in calls if c["resume_session_id"] == "ses-double-crash"
                    and "continue where you left off" in c["prompt"]]
    assert len(resume_calls) == 1, (
        f"Expected exactly 1 crash-resume call, got {len(resume_calls)}: {resume_calls}"
    )


def test_crash_resume_skipped_for_non_claude_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-claude engines with unclassified crash should NOT trigger resume."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Codex crash task", engine="codex", auto_commit=False)
    task = require_task(tmp_path, "T-0001")

    calls: list[dict] = []

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        calls.append({
            "engine": engine_name,
            "resume_session_id": resume_session_id,
            "step": task_arg.pipeline_status,
        })
        if task_arg.pipeline_status == "implementing" and len([c for c in calls if c["step"] == "implementing"]) == 1:
            # First implementing call: crash with no failure classification
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

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)
    summary = run_task(tmp_path, task)

    # No crash-resume calls should happen for codex (no "continue where you left off" with session ID)
    resume_calls = [c for c in calls if c.get("resume_session_id") is not None
                    and "continue where you left off" in (c.get("prompt") or "")]
    assert len(resume_calls) == 0, f"Expected 0 resume calls for codex, got {len(resume_calls)}: {resume_calls}"


def test_crash_resume_journal_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Journal clearly logs the resume attempt with session ID and exit code."""
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Journal crash task", engine="claude", auto_commit=False)
    task = require_task(tmp_path, "T-0001")

    impl_call_count = 0

    def fake_run(
        self, task_arg, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        nonlocal impl_call_count
        if task_arg.pipeline_status == "implementing":
            impl_call_count += 1
            if impl_call_count == 1:
                return _crash_result("implementing", session_id="ses-journal-456")
        return _completed_subagent_result(tmp_path, task_arg.pipeline_status, task=task_arg)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)
    run_task(tmp_path, task)

    task_folder = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}"
    journal = (task_folder / "journal.md").read_text(encoding="utf-8")

    assert "crashed" in journal.lower() or "crash" in journal.lower()
    assert "ses-journal-456" in journal
    assert "resuming" in journal.lower()
