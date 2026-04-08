import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from litehive.config import LitehiveConfig, VALID_ENGINE_NAMES, ensure_workspace
from litehive.engines import get_engine
from litehive.engines.base import CLIExecutionResult
from litehive.models import StageResultSubmission


INTEGRATION_ENV = "LITEHIVE_INTEGRATION_ENGINES"
TIMEOUT_ENV = "LITEHIVE_INTEGRATION_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 120
ENGINE_MATRIX = tuple(sorted(VALID_ENGINE_NAMES))


def enabled_integration_engines() -> set[str]:
    raw = os.environ.get(INTEGRATION_ENV, "")
    if not raw.strip():
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def require_real_engine(engine_name: str) -> None:
    enabled = enabled_integration_engines()
    if not enabled:
        pytest.skip(
            f"real engine integration tests are opt-in; set {INTEGRATION_ENV}="
            + ",".join(ENGINE_MATRIX)
        )
    if engine_name not in enabled:
        pytest.skip(
            f"{engine_name} integration test not selected; set {INTEGRATION_ENV} to include it"
        )
    capabilities = get_engine(engine_name).detect_capabilities()
    if not capabilities.available:
        pytest.skip(f"{engine_name} binary is not available on PATH")


def integration_workspace(root: Path) -> Path:
    ensure_workspace(root, LitehiveConfig(default_engine="codex"))
    return root


def smoke_prompt(engine_name: str) -> str:
    summary = f"{engine_name} integration smoke"
    return (
        "Reply with exactly these sections and nothing else. "
        "Do not call tools.\n"
        f"SUMMARY: {summary}\n"
        "STAGE_RESULT:\n"
        + json.dumps(
            {
                "verdict": "pass",
                "summary": summary,
                "files_changed": [],
                "tests": {"added": 0, "passing": 0},
                "warnings": [],
                "acceptance_criteria": [],
            }
        )
    )


def execute_engine_prompt(
    engine_name: str,
    *,
    prompt: str,
    cwd: Path,
) -> tuple[object, CLIExecutionResult]:
    engine = get_engine(engine_name)
    max_turns = 1 if engine_name == "claude" else None
    invocation = engine.finalize_invocation(
        engine.build_invocation(prompt, cwd, max_turns=max_turns)
    )
    timeout_seconds = int(os.environ.get(TIMEOUT_ENV, str(DEFAULT_TIMEOUT_SECONDS)))
    try:
        completed = subprocess.run(
            invocation.argv,
            cwd=invocation.cwd,
            env=invocation.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_tail = (exc.stdout or "")[-800:]
        stderr_tail = (exc.stderr or "")[-800:]
        pytest.fail(
            f"{engine_name} timed out after {timeout_seconds}s\n"
            f"argv: {list(invocation.argv)!r}\n"
            f"stdout_tail:\n{stdout_tail}\n"
            f"stderr_tail:\n{stderr_tail}"
        )
    sandboxed, sandbox_summary = engine.sandbox_details()
    return engine, CLIExecutionResult(
        adapter=engine.name,
        argv=invocation.argv,
        cwd=invocation.cwd,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        sandboxed=sandboxed,
        sandbox_summary=sandbox_summary,
    )


def _assistant_transcript(transcript: str) -> str:
    return transcript.partition("\n\n[stderr]\n")[0].strip()


def extract_stage_result_submission(transcript: str) -> StageResultSubmission:
    assistant_text = _assistant_transcript(transcript)
    marker = "STAGE_RESULT:"
    assert marker in assistant_text, assistant_text
    payload_text = assistant_text.split(marker, 1)[1].strip()
    return StageResultSubmission.model_validate(json.loads(payload_text))


def assert_successful_stage_result(engine_name: str, *, cwd: Path) -> None:
    require_real_engine(engine_name)
    engine, execution = execute_engine_prompt(
        engine_name,
        prompt=smoke_prompt(engine_name),
        cwd=cwd,
    )
    assert execution.exit_code == 0, execution.transcript
    transcript = engine.render_transcript(execution)
    submission = extract_stage_result_submission(transcript)
    assert submission.verdict == "pass"
    assert submission.summary == f"{engine_name} integration smoke"
    report = engine.parse_stage_report(
        task_id="T-INTEGRATION",
        step="implementing",
        execution=execution,
        subagent_status="completed",
    )
    assert report.verdict == "pass"
    assert report.summary == submission.summary
    assert report.tests == {"added": 0, "passing": 0}


def assert_nudge_verdict_submission(engine_name: str, *, cwd: Path) -> None:
    """Verify the nudge flow: run engine, then nudge to submit verdict via CLI."""
    from litehive.engines import extract_engine_continuation
    from litehive.tasks import create_task, load_task_thread, require_task, set_active_task

    require_real_engine(engine_name)

    task = create_task(cwd, title=f"{engine_name} nudge task", auto_commit=False)
    set_active_task(cwd, task.id)

    # Step 1: initial run
    engine, first_run = execute_engine_prompt(
        engine_name,
        prompt="Reply with: I am done. Do not call any tools.",
        cwd=cwd,
    )
    assert first_run.exit_code == 0, first_run.stderr

    # Step 2: nudge — submit verdict via litehive report CLI
    _, nudge_run = execute_engine_prompt(
        engine_name,
        prompt=(
            f"Run this command exactly once:\n\n"
            f"  litehive report --task-id {task.id} --verdict pass --role swe "
            f"--step implementing --workspace {cwd} "
            f'--message "{engine_name} nudge verdict submitted"'
        ),
        cwd=cwd,
    )
    assert nudge_run.exit_code == 0, nudge_run.stderr

    # Step 3: verify verdict persisted
    thread = load_task_thread(cwd, require_task(cwd, task.id))
    verdicts = [c for c in thread if c.verdict != "comment"]
    assert len(verdicts) >= 1, f"Expected verdict from {engine_name}, got: {thread}"
    assert verdicts[-1].verdict == "pass"
    assert verdicts[-1].role == "swe"


def cli_command(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "litehive.main", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
