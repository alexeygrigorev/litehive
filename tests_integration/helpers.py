import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from litehive.config.loading import load_config
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.config.model import VALID_ENGINE_NAMES
from heru import extract_engine_continuation, get_engine
from heru.base import CLIExecutionResult


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


def _engine_quota_block_reason(engine_name: str) -> str | None:
    """Check if an engine's quota is too high to run integration tests."""
    try:
        if engine_name == "codex":
            from heru.quota.codex_quota import codex_quota_block_reason
            return codex_quota_block_reason()
        if engine_name == "claude":
            from heru.quota.claude_quota import claude_quota_block_reason
            return claude_quota_block_reason()
        if engine_name == "copilot":
            from heru.quota.copilot_quota import copilot_quota_block_reason
            return copilot_quota_block_reason()
        if engine_name in ("goz", "opencode"):
            from heru.quota.zai_quota import zai_quota_block_reason
            return zai_quota_block_reason()
    except Exception:
        pass
    return None


def require_real_engine(engine_name: str) -> None:
    engine = get_engine(engine_name)
    if not engine.is_available():
        pytest.skip(f"{engine_name} binary not available on PATH")
    quota_reason = _engine_quota_block_reason(engine_name)
    if quota_reason:
        pytest.skip(f"{engine_name} quota too high: {quota_reason}")


def integration_workspace(root: Path) -> Path:
    ensure_workspace(
        root,
        LitehiveConfig(
            default_engine="codex",
            opencode_model="zai-coding-plan/glm-5.1",
            gemini_model="gemini-2.5-flash-lite",
            claude_model="claude-sonnet-4-20250514",
        ),
    )
    return root


def sandboxed_integration_workspace(root: Path) -> Path:
    """Integration workspace with bwrap sandboxing enabled and per-engine
    policies wired up (binds for ~/.codex, ~/.claude, etc., plus
    setenv HOME and CODEX_HOME). Used by tests that must exercise the
    full sandbox path on real engines. Skips if the operator's session
    dirs don't exist on this host."""
    import os as _os

    from litehive.config.model import ExternalEngineSandboxConfig, ExternalEngineSandboxPolicy

    home = _os.path.expanduser("~")
    nvm = f"{home}/.nvm/versions/node/v24.13.1"
    codex_dir = f"{home}/.codex"
    claude_dir = f"{home}/.claude"
    copilot_dir = f"{home}/.copilot"
    gh_config = f"{home}/.config/gh"  # copilot auth lives here via `gh auth login`
    gemini_dir = f"{home}/.gemini"
    goz_dir = f"{home}/.goz"
    goz_config = f"{home}/.config/goz"
    goz_bin = f"{home}/bin"
    local_bin = f"{home}/.local/bin"  # goz wrapper script uses uv from here
    opencode_dir = f"{home}/.config/opencode"

    engine_policies: dict[str, ExternalEngineSandboxPolicy] = {}
    setenv_home = {"HOME": home}

    if Path(nvm).exists() and Path(codex_dir).exists():
        # ~/.codex must be writable: codex persists trusted-project state to
        # config.toml on every run and exits non-zero when it cannot.
        engine_policies["codex"] = ExternalEngineSandboxPolicy(
            enabled=True,
            network_mode="host",
            extra_ro_binds=[nvm],
            extra_rw_binds=[codex_dir],
            setenv={**setenv_home, "CODEX_HOME": codex_dir},
        )
    if Path(nvm).exists() and Path(claude_dir).exists():
        # ~/.claude is rewritten on every run (telemetry, statsig, session
        # state). Read-only causes intermittent rc!=0.
        engine_policies["claude"] = ExternalEngineSandboxPolicy(
            enabled=True,
            network_mode="host",
            extra_ro_binds=[nvm],
            extra_rw_binds=[claude_dir],
            setenv=setenv_home,
        )
    if Path(nvm).exists() and Path(copilot_dir).exists():
        copilot_ro_binds = [nvm]
        if Path(gh_config).exists():
            copilot_ro_binds.append(gh_config)
        engine_policies["copilot"] = ExternalEngineSandboxPolicy(
            enabled=True,
            network_mode="host",
            extra_ro_binds=copilot_ro_binds,
            extra_rw_binds=[copilot_dir],
            setenv=setenv_home,
        )
    if Path(nvm).exists() and Path(gemini_dir).exists():
        # ~/.gemini must be writable: gemini-cli rewrites state files
        # (installation_id, settings.json scratch updates, oauth refresh,
        # etc.) at startup and hangs indefinitely when the dir is read-only.
        engine_policies["gemini"] = ExternalEngineSandboxPolicy(
            enabled=True,
            network_mode="host",
            extra_ro_binds=[nvm],
            extra_rw_binds=[gemini_dir],
            setenv=setenv_home,
        )
    if Path(goz_bin).exists() and Path(goz_dir).exists():
        goz_ro_binds = [goz_bin]
        if Path(goz_config).exists():
            goz_ro_binds.append(goz_config)
        if Path(local_bin).exists():
            goz_ro_binds.append(local_bin)
        # goz wrapper script `exec uv run --project /home/alexey/git/goz python -m goz`
        # needs the source project bind-mounted so `python -m goz` can import it,
        # plus uv's managed python toolchain dir so .venv's python symlink resolves.
        goz_project = f"{home}/git/goz"
        if Path(goz_project).exists():
            goz_ro_binds.append(goz_project)
        uv_python_dir = f"{home}/.local/share/uv/python"
        if Path(uv_python_dir).exists():
            goz_ro_binds.append(uv_python_dir)
        # ~/.goz must be writable because goz persists a session file per
        # invocation to ~/.goz/sessions/<id>.json. A --ro-bind here causes
        # the smoke test to exit 1 after the successful reply.
        engine_policies["goz"] = ExternalEngineSandboxPolicy(
            enabled=True,
            network_mode="host",
            extra_ro_binds=goz_ro_binds,
            extra_rw_binds=[goz_dir],
            # UV_FROZEN: the goz wrapper runs `uv run --project`, and uv
            # triggers a re-resolve on any env drift unless told to trust
            # the existing lockfile. The source project dir is read-only
            # inside the sandbox, so a re-resolve would fail writing uv.lock.
            setenv={**setenv_home, "UV_FROZEN": "1"},
        )
    if Path(nvm).exists() and Path(opencode_dir).exists():
        engine_policies["opencode"] = ExternalEngineSandboxPolicy(
            enabled=True,
            network_mode="host",
            extra_ro_binds=[nvm],
            extra_rw_binds=[opencode_dir],
            setenv=setenv_home,
        )

    ensure_workspace(
        root,
        LitehiveConfig(
            default_engine="codex",
            opencode_model="zai-coding-plan/glm-5.1",
            gemini_model="gemini-2.5-flash-lite",
            claude_model="claude-sonnet-4-20250514",
            external_engine_sandbox=ExternalEngineSandboxConfig(
                enabled=True,
                backend="bubblewrap",
                runtime_binary="bwrap",
                engine_policies=engine_policies,
            ),
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
    sandboxed: bool = False,
    role: str = "swe",
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
        "claude": config.claude_model,
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
    run_cwd = invocation.cwd
    run_env = invocation.env
    sandbox_summary_override: str | None = None
    sandbox_applied = False
    if sandboxed:
        from litehive.agents.sandbox import SandboxLauncher
        from heru.base import CLIInvocation as _HeruCLIInvocation

        launcher = SandboxLauncher(cwd, config)
        summary = launcher.policy_summary(engine_name, role)
        if not summary.enabled:
            pytest.skip(
                f"sandbox not enabled in config for engine {engine_name!r} — "
                "set external_engine_sandbox.enabled=true and engine_policies.<engine>.enabled=true"
            )
        heru_invocation = _HeruCLIInvocation(
            argv=tuple(argv),
            cwd=Path(run_cwd),
            env=dict(run_env),
            stdin_data=None,
        )
        wrapped = launcher.wrap_invocation(engine_name, argv[0], heru_invocation, role=role)
        argv = list(wrapped.argv)
        run_cwd = wrapped.cwd
        run_env = dict(wrapped.env)
        sandbox_summary_override = summary.summary
        sandbox_applied = True
    timeout_seconds = int(os.environ.get(TIMEOUT_ENV, str(DEFAULT_TIMEOUT_SECONDS)))
    try:
        completed = subprocess.run(
            argv,
            cwd=run_cwd,
            env=run_env,
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
            f"{engine_name} timed out after {timeout_seconds}s (sandboxed={sandbox_applied})\n"
            f"argv: {argv!r}\n"
            f"stdout_tail:\n{stdout_tail}\n"
            f"stderr_tail:\n{stderr_tail}"
        )
    if sandbox_applied:
        sandboxed_flag = True
        sandbox_summary = sandbox_summary_override or ""
    else:
        sandboxed_flag, sandbox_summary = engine.sandbox_details()
    return engine, CLIExecutionResult(
        adapter=engine.name,
        argv=tuple(argv),
        cwd=Path(run_cwd),
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        sandboxed=sandboxed_flag,
        sandbox_summary=sandbox_summary,
    )


def _assistant_transcript(transcript: str) -> str:
    return transcript.partition("\n\n[stderr]\n")[0].strip()


def prepare_smoke_session(engine_name: str, *, cwd: Path, sandboxed: bool = False) -> SmokeSession:
    from litehive.state.records import create_task, require_task, save_task
    from litehive.tasks.queue import set_active_task

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
        sandboxed=sandboxed,
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
    from litehive.state.records import require_task
    from litehive.tasks.reports import load_task_thread

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
