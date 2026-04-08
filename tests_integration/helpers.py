import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from litehive.config import LitehiveConfig, VALID_ENGINE_NAMES, ensure_workspace, load_config
from litehive.engines import extract_engine_continuation, get_engine
from litehive.engines.base import CLIExecutionResult


INTEGRATION_ENV = "LITEHIVE_INTEGRATION_ENGINES"
TIMEOUT_ENV = "LITEHIVE_INTEGRATION_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 30
ENGINE_MATRIX = tuple(sorted(VALID_ENGINE_NAMES))


@dataclass(frozen=True, slots=True)
class SmokeSession:
    engine_name: str
    cwd: Path
    task_id: str
    engine: object
    execution: CLIExecutionResult
    resume_session_id: str | None = None


def enabled_integration_engines() -> set[str]:
    raw = os.environ.get(INTEGRATION_ENV, "")
    if not raw.strip():
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def require_real_engine(engine_name: str) -> None:
    engine = get_engine(engine_name)
    if not engine.is_available():
        pytest.skip(f"{engine_name} binary not available on PATH")


def integration_workspace(root: Path) -> Path:
    ensure_workspace(
        root,
        LitehiveConfig(
            default_engine="codex",
            opencode_model="zai-coding-plan/glm-5.1",
            gemini_model="gemini-2.5-flash-lite",
            claude_enabled=True,
            claude_model="claude-sonnet-4-20250514",
        ),
    )
    return root


def smoke_prompt(engine_name: str) -> str:
    return f"Reply with exactly: {engine_name} integration smoke."


def execute_engine_prompt(
    engine_name: str,
    *,
    prompt: str,
    cwd: Path,
    max_turns: int | None = None,
    resume_session_id: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[object, CLIExecutionResult]:
    engine = get_engine(engine_name)
    if max_turns is None and engine_name == "claude":
        max_turns = 1
    config = load_config(cwd)
    model = {
        "codex": config.codex_model,
        "opencode": config.opencode_model,
        "goz": config.goz_model,
        "gemini": config.gemini_model,
        "copilot": config.copilot_model,
        "claude": config.claude_model if config.claude_enabled else None,
    }.get(engine_name)
    invocation = engine.finalize_invocation(
        engine.build_invocation(
            prompt,
            cwd,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
            extra_env=extra_env,
        )
    )
    argv = list(invocation.argv)
    if engine_name == "copilot":
        argv.extend(
            [
                "--reasoning-effort",
                "low",
                "--max-autopilot-continues",
                "0",
                "--disable-builtin-mcps",
                "--no-custom-instructions",
            ]
        )
    elif engine_name == "gemini":
        argv.extend(["--sandbox", "false"])
    timeout_seconds = int(os.environ.get(TIMEOUT_ENV, str(DEFAULT_TIMEOUT_SECONDS)))
    try:
        completed = subprocess.run(
            argv,
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
            f"argv: {argv!r}\n"
            f"stdout_tail:\n{stdout_tail}\n"
            f"stderr_tail:\n{stderr_tail}"
        )
    sandboxed, sandbox_summary = engine.sandbox_details()
    return engine, CLIExecutionResult(
        adapter=engine.name,
        argv=tuple(argv),
        cwd=invocation.cwd,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        sandboxed=sandboxed,
        sandbox_summary=sandbox_summary,
    )


def _assistant_transcript(transcript: str) -> str:
    return transcript.partition("\n\n[stderr]\n")[0].strip()


def prepare_smoke_session(engine_name: str, *, cwd: Path) -> SmokeSession:
    from litehive.tasks import create_task, require_task, save_task, set_active_task

    require_real_engine(engine_name)
    task = create_task(cwd, title=f"{engine_name} nudge task", auto_commit=False)
    task = require_task(cwd, task.id)
    task.pipeline_status = "implementing"
    save_task(cwd, task)
    set_active_task(cwd, task.id)
    engine, execution = execute_engine_prompt(
        engine_name,
        prompt=smoke_prompt(engine_name),
        cwd=cwd,
    )
    assert execution.exit_code == 0, execution.transcript
    continuation = extract_engine_continuation(engine_name, execution)
    resume_session_id = (
        continuation.session_id
        if continuation is not None and continuation.session_id
        else "latest"
        if engine_name == "copilot"
        else None
    )
    return SmokeSession(
        engine_name=engine_name,
        cwd=cwd,
        task_id=task.id,
        engine=engine,
        execution=execution,
        resume_session_id=resume_session_id,
    )


def assert_successful_smoke_session(session: SmokeSession) -> None:
    transcript = _assistant_transcript(session.engine.render_transcript(session.execution))
    assert transcript.strip(), f"Expected assistant output from {session.engine_name}"


def assert_successful_smoke_prompt(engine_name: str, *, cwd: Path) -> None:
    assert_successful_smoke_session(prepare_smoke_session(engine_name, cwd=cwd))


def assert_nudge_verdict_submission(
    engine_name: str,
    *,
    cwd: Path | None = None,
    smoke_session: SmokeSession | None = None,
) -> None:
    """Verify the nudge flow: run engine, then nudge to submit verdict via CLI."""
    from litehive.tasks import load_task_thread, require_task

    session = smoke_session
    if session is None:
        assert cwd is not None, "cwd is required when smoke_session is not provided"
        session = prepare_smoke_session(engine_name, cwd=cwd)
    else:
        assert session.engine_name == engine_name, (session.engine_name, engine_name)

    report_command = (
        "litehive report "
        "--verdict pass "
        "--role swe "
        "--message ok"
    )

    # Step 2: nudge — submit verdict via litehive report CLI
    _, nudge_run = execute_engine_prompt(
        engine_name,
        prompt=f"Run {report_command} exactly once.",
        cwd=session.cwd,
        max_turns=2 if engine_name == "claude" else None,
        resume_session_id=(
            session.resume_session_id if engine_name not in {"gemini", "goz", "opencode"} else None
        ),
        extra_env={"LITEHIVE_TASK_ID": session.task_id},
    )
    assert nudge_run.exit_code == 0, nudge_run.stderr

    # Step 3: verify verdict persisted
    thread = load_task_thread(session.cwd, require_task(session.cwd, session.task_id))
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
