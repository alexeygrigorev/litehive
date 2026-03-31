import argparse
from pathlib import Path
import subprocess

import pytest

from litehive.engines import get_engine
from litehive.cli import _cmd_run
from litehive.config import ensure_workspace
from litehive.external_cli import CLIExecutionResult, parse_stage_report_text
from litehive.models import StageReport, SubagentRef
from litehive.runtime import resolve_next_task, run_next_task
from litehive.runner import TaskExecutionRunner
from litehive.subagents import SubagentResult, stage_report_from_subagent
from litehive.tasks import create_task, list_tasks, load_state, set_active_task


def test_ensure_workspace_creates_layout(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    assert (tmp_path / ".litehive" / "config.yaml").exists()
    assert (tmp_path / ".litehive" / "state.yaml").exists()
    assert (tmp_path / ".litehive" / "tasks").exists()


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


def test_engine_capabilities_report_availability_and_contract_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")

    codex = get_engine("codex").detect_capabilities()
    opencode = get_engine("opencode").detect_capabilities()

    assert codex.available is True
    assert codex.supports_model_override is False
    assert codex.transcript_format == "text"
    assert opencode.available is True
    assert opencode.supports_model_override is True
    assert opencode.strips_environment is True


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


def test_run_next_task_no_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    summary = run_next_task(tmp_path)

    assert summary.task is None
    assert summary.result is None


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


def test_cmd_run_dry_run_shows_task_and_engine_without_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued task", engine="opencode")

    def fail_run_next_task(root: Path) -> None:
        raise AssertionError(f"run_next_task should not be called for dry-run: {root}")

    monkeypatch.setattr("litehive.cli.run_next_task", fail_run_next_task)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 Queued task" in output
    assert "engine: opencode" in output
    assert load_state(tmp_path).queue == ["T-0001"]
