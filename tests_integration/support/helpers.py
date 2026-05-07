import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from heru import (
    classify_execution_limit,
    extract_engine_continuation,
    get_engine,
    resolve_engine_resume_session_id,
    resume_safe_model_override,
)
from heru.quota import (
    check_claude_quota,
    check_codex_quota,
    check_copilot_quota,
    check_zai_quota,
    usage_limit_block_reason,
)
from heru.quota._shared import UsageStatus
from litehive.config.loading import load_config
from litehive.config.model import LitehiveConfig
from litehive.config.paths import litehive_root
from litehive.config.workspace import create_workspace
from litehive.domain.common import PipelineStatus
from litehive.config.model import VALID_ENGINE_NAMES
from litehive.workspace import Workspace
from heru.base import CLIExecutionResult


INTEGRATION_ENV = "LITEHIVE_INTEGRATION_ENGINES"
TIMEOUT_ENV = "LITEHIVE_INTEGRATION_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 30
ENGINE_MATRIX = tuple(sorted(VALID_ENGINE_NAMES))
_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOZ_TRANSIENT_ERROR_MARKERS = ("ResponseNotRead",)
_LITEHIVE_SESSION_ENV_VARS = (
    "LITEHIVE_TASK_ID",
    "LITEHIVE_WORKSPACE_ROOT",
    "LITEHIVE_AGENT_ROLE",
    "LITEHIVE_SUBAGENT_ID",
    "LITEHIVE_STAGE",
)
_REAL_XDG_ENV_VARS = (
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)


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
        checker = {
            "codex": check_codex_quota,
            "claude": check_claude_quota,
            "copilot": check_copilot_quota,
            "goz": check_zai_quota,
            "opencode": check_zai_quota,
        }.get(engine_name)
        if checker is None:
            return None
        # heru's per-engine quota status classes are structurally compatible
        # with UsageStatus (same field shape) but do not inherit from it; cast
        # so the boundary is explicit rather than suppressed.
        return usage_limit_block_reason(engine_name, cast(UsageStatus, checker()))
    except Exception:
        pass
    return None


def require_real_engine(engine_name: str) -> None:
    requested_engines = enabled_integration_engines()
    if requested_engines and engine_name not in requested_engines:
        pytest.skip(f"{engine_name} not selected in {INTEGRATION_ENV}")
    engine = get_engine(engine_name)
    if not engine.is_available():
        pytest.skip(f"{engine_name} binary not available on PATH")
    quota_reason = _engine_quota_block_reason(engine_name)
    if quota_reason and engine_name not in requested_engines:
        pytest.skip(f"{engine_name} quota too high: {quota_reason}")


def integration_workspace(root: Path) -> Path:
    create_workspace(
        root,
        LitehiveConfig(
            default_engine="codex",
            opencode_model="zai-coding-plan/glm-5-turbo",
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
    claude_config = f"{home}/.claude.json"
    copilot_dir = f"{home}/.copilot"
    gh_config = f"{home}/.config/gh"  # copilot auth lives here via `gh auth login`
    gemini_dir = f"{home}/.gemini"
    goz_dir = f"{home}/.goz"
    goz_config = f"{home}/.config/goz"
    goz_bin = f"{home}/bin"
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
        claude_rw_binds = [claude_dir]
        if Path(claude_config).exists():
            claude_rw_binds.append(claude_config)
        engine_policies["claude"] = ExternalEngineSandboxPolicy(
            enabled=True,
            network_mode="host",
            extra_ro_binds=[nvm],
            extra_rw_binds=claude_rw_binds,
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
        # The local launcher executes the project's virtualenv python directly,
        # and the editable install resolves against the source tree.
        goz_project = f"{home}/git/goz"
        if Path(goz_project).exists():
            goz_ro_binds.append(goz_project)
        # The goz virtualenv's python is a symlink into uv's managed Python
        # install, so the interpreter target itself must also be visible in
        # the sandbox.
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
            setenv=setenv_home,
        )
    if Path(nvm).exists() and Path(opencode_dir).exists():
        engine_policies["opencode"] = ExternalEngineSandboxPolicy(
            enabled=True,
            network_mode="host",
            extra_ro_binds=[nvm],
            extra_rw_binds=[opencode_dir],
            setenv=setenv_home,
        )

    create_workspace(
        root,
        LitehiveConfig(
            default_engine="codex",
            opencode_model="zai-coding-plan/glm-5-turbo",
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


def litehive_python_shell_prefix() -> str:
    return f"PYTHONPATH={shlex.quote(str(_REPO_ROOT))}${{PYTHONPATH:+:$PYTHONPATH}} "


def _run_engine_subprocess(
    argv: list[str],
    *,
    cwd: str | Path,
    env: dict[str, str],
    timeout_seconds: int,
    allow_timeout: bool,
    stop_when: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    if stop_when is None:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout_seconds
    process_group = process.pid

    while True:
        if stop_when():
            if process.poll() is None:
                os.killpg(process_group, signal.SIGTERM)
                try:
                    stdout, stderr = process.communicate(timeout=1.0)
                except subprocess.TimeoutExpired:
                    os.killpg(process_group, signal.SIGKILL)
                    stdout, stderr = process.communicate(timeout=1.0)
            else:
                stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=stdout, stderr=stderr)

        try:
            stdout, stderr = process.communicate(timeout=0.2)
            return subprocess.CompletedProcess(
                args=argv, returncode=process.returncode or 0, stdout=stdout, stderr=stderr
            )
        except subprocess.TimeoutExpired as exc:
            if time.monotonic() < deadline:
                continue
            os.killpg(process_group, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=1.0)
            if allow_timeout:
                return subprocess.CompletedProcess(args=argv, returncode=124, stdout=stdout, stderr=stderr)
            raise subprocess.TimeoutExpired(
                argv, timeout_seconds, output=stdout or exc.stdout, stderr=stderr or exc.stderr
            )


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
    allow_timeout: bool = False,
    stop_when: Callable[[], bool] | None = None,
) -> tuple[object, CLIExecutionResult]:
    engine = get_engine(engine_name)
    if max_turns is None and engine_name == "claude":
        max_turns = 1
    config = load_config(cwd)
    model = {
        "opencode": config.opencode_model,
        "goz": config.goz_model,
        "gemini": config.gemini_model,
        "copilot": config.copilot_model,
        "claude": config.claude_model,
    }.get(engine_name)
    model = resume_safe_model_override(engine_name, model, resume_session_id=resume_session_id)
    invocation = engine.finalize_invocation(
        engine.build_invocation(
            prompt,
            cwd,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
            extra_env={
                "LITEHIVE_PYTHON_PATH": sys.executable,
                # Opencode needs access to the operator's real XDG config,
                # and copilot relies on the operator's GitHub CLI auth under
                # the real XDG config dir. Nested `litehive` CLI calls still
                # keep using the test process's Litehive home so they write
                # into the same DB.
                "LITEHIVE_HOME": str(litehive_root()),
                **(
                    {
                        key: os.environ[f"LITEHIVE_REAL_{key}"]
                        for key in _REAL_XDG_ENV_VARS
                        if engine_name in {"copilot", "opencode"} and os.environ.get(f"LITEHIVE_REAL_{key}")
                    }
                ),
                **(extra_env or {}),
            },
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
                "--allow-all-paths",
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
        from litehive.sandbox.launcher import DockerSandboxLauncher
        from litehive.workspace import Workspace
        from heru.base import CLIInvocation as _HeruCLIInvocation

        launcher = DockerSandboxLauncher(Workspace.from_path(cwd), config)
        summary = launcher.policy_summary(engine_name)
        if not summary.enabled:
            pytest.skip(
                f"sandbox not enabled in config for engine {engine_name!r} — "
                "set external_engine_sandbox.enabled=true"
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
    completed: subprocess.CompletedProcess[str] | None = None
    attempts = 2 if engine_name == "goz" else 1
    for attempt in range(attempts):
        try:
            completed = _run_engine_subprocess(
                argv,
                cwd=run_cwd,
                env=run_env,
                timeout_seconds=timeout_seconds,
                allow_timeout=allow_timeout,
                stop_when=stop_when,
            )
        except subprocess.TimeoutExpired as exc:
            if allow_timeout:
                stdout_str = exc.stdout if isinstance(exc.stdout, str) else ""
                stderr_str = exc.stderr if isinstance(exc.stderr, str) else ""
                completed = subprocess.CompletedProcess(
                    args=argv,
                    returncode=124,
                    stdout=stdout_str,
                    stderr=stderr_str,
                )
                break
            stdout_tail = (exc.stdout or "")[-800:]
            stderr_tail = (exc.stderr or "")[-800:]
            pytest.fail(
                f"{engine_name} timed out after {timeout_seconds}s (sandboxed={sandbox_applied})\n"
                f"argv: {argv!r}\n"
                f"stdout_tail:\n{stdout_tail}\n"
                f"stderr_tail:\n{stderr_tail}"
            )
        combined_output = f"{completed.stdout}\n{completed.stderr}"
        if (
            engine_name == "goz"
            and completed.returncode != 0
            and attempt + 1 < attempts
            and any(marker in combined_output for marker in _GOZ_TRANSIENT_ERROR_MARKERS)
        ):
            time.sleep(0.5)
            continue
        break
    assert completed is not None
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


def _execution_limit_reason(execution: CLIExecutionResult) -> str | None:
    combined = execution.transcript or f"{execution.stdout}\n{execution.stderr}"
    limit_reason = classify_execution_limit(combined)
    if limit_reason is not None:
        return limit_reason
    normalized = " ".join(combined.lower().split())
    if any(
        marker in normalized
        for marker in (
            '"errortype":"quota"',
            '"statuscode":402',
            "status code 402",
            "no quota",
        )
    ):
        return "usage limit reached"
    return None


def _skip_if_execution_limited(engine_name: str, execution: CLIExecutionResult) -> None:
    limit_reason = _execution_limit_reason(execution)
    if limit_reason is not None:
        pytest.skip(f"{engine_name} became unavailable during integration test: {limit_reason}")


def prepare_smoke_session(engine_name: str, *, cwd: Path, sandboxed: bool = False) -> SmokeSession:
    from litehive.state.records import create_task, require_task, save_task
    from litehive.tasks.queue import set_active_task

    require_real_engine(engine_name)
    task = create_task(cwd, title=f"{engine_name} nudge task", auto_commit=False)
    task = require_task(cwd, task.id)
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    save_task(cwd, task)
    set_active_task(Workspace.from_path(cwd), task.id)
    engine, execution = execute_engine_prompt(
        engine_name,
        prompt=smoke_prompt(engine_name),
        cwd=cwd,
        sandboxed=sandboxed,
    )
    _skip_if_execution_limited(engine_name, execution)
    assert execution.exit_code == 0, execution.transcript
    continuation = extract_engine_continuation(engine_name, execution)
    resume_session_id = resolve_engine_resume_session_id(
        engine_name,
        continuation,
        prefer_latest=True,
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
    render_transcript = getattr(session.engine, "render_transcript", None)
    assert callable(render_transcript), f"engine for {session.engine_name} cannot render transcripts"
    rendered = render_transcript(session.execution)
    assert isinstance(rendered, str)
    transcript = _assistant_transcript(rendered)
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
    from litehive.agents.session_store import SubagentArtifactPayload, subagent_artifacts
    from litehive.state.records import require_task
    from litehive.tasks.activity import load_task_activity
    from litehive.workspace import Workspace

    session = smoke_session
    if session is None:
        assert cwd is not None, "cwd is required when smoke_session is not provided"
        session = prepare_smoke_session(engine_name, cwd=cwd)
    else:
        assert session.engine_name == engine_name, (session.engine_name, engine_name)

    subagent_id = "SI-nudge-swe"
    subagent_artifacts(Workspace.from_path(session.cwd), session.task_id, subagent_id).save(
        session=SubagentArtifactPayload({"id": subagent_id, "role": "swe", "engine": engine_name, "status": "running"}),
    )
    report_command = (
        f"{litehive_python_shell_prefix()}"
        f"LITEHIVE_AGENT_ROLE=swe LITEHIVE_SUBAGENT_ID={subagent_id} "
        f"{shlex.quote(sys.executable)} -m litehive.main report "
        "--verdict pass "
        "--stage implementing "
        f"--task-id {session.task_id} "
        f"--workspace {shlex.quote(str(session.cwd))} "
        "--message ok"
    )

    def verdict_persisted() -> bool:
        thread = load_task_activity(Workspace.from_path(session.cwd), require_task(session.cwd, session.task_id))
        verdicts = [c for c in thread if c.verdict != "comment"]
        return bool(
            verdicts
            and verdicts[-1].verdict == "pass"
            and verdicts[-1].role == "swe"
            and verdicts[-1].source_subagent_id == subagent_id
        )

    # Step 2: nudge — submit verdict via litehive report CLI
    _, nudge_run = execute_engine_prompt(
        engine_name,
        prompt=f"Run this shell command exactly once, then stop: {report_command}",
        cwd=session.cwd,
        max_turns=1,
        resume_session_id=session.resume_session_id,
        extra_env={
            "LITEHIVE_TASK_ID": session.task_id,
            "LITEHIVE_AGENT_ROLE": "swe",
            "LITEHIVE_SUBAGENT_ID": subagent_id,
            "LITEHIVE_STAGE": "implementing",
        },
        stop_when=verdict_persisted,
    )
    _skip_if_execution_limited(engine_name, nudge_run)

    # Step 3: verify verdict persisted
    deadline = time.monotonic() + 3.0
    while True:
        thread = load_task_activity(Workspace.from_path(session.cwd), require_task(session.cwd, session.task_id))
        verdicts = [c for c in thread if c.verdict != "comment"]
        if verdicts:
            assert verdicts[-1].verdict == "pass"
            assert verdicts[-1].role == "swe"
            assert verdicts[-1].source_subagent_id == subagent_id
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    assert nudge_run.exit_code == 0, nudge_run.stderr
    assert len(verdicts) >= 1, (
        f"Expected verdict from {engine_name}, got: {thread}\nstdout:\n{nudge_run.stdout}\nstderr:\n{nudge_run.stderr}"
    )
    assert verdicts[-1].verdict == "pass"
    assert verdicts[-1].role == "swe"
    assert verdicts[-1].source_subagent_id == subagent_id


def cli_command(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in _LITEHIVE_SESSION_ENV_VARS:
        env.pop(key, None)
    env["LITEHIVE_PYTHON_PATH"] = sys.executable
    return subprocess.run(
        [sys.executable, "-m", "litehive.main", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
