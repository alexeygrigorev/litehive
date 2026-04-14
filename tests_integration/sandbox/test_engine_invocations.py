"""Integration tests that run each real engine under the bwrap sandbox.

These tests exercise the full adapter → SandboxLauncher → bwrap → engine
path on the real binaries. They confirm that each engine's session state
is properly bind-mounted into the sandbox and that the engine can still
reach a clean exit with HOME remapped to the operator home.

Skip conditions:
- The engine binary is not available on PATH (require_real_engine).
- The workspace config has `external_engine_sandbox.enabled=false` or
  the engine's policy is disabled — execute_engine_prompt(sandboxed=True)
  will pytest.skip with a clear message.
- The caller's quota for the engine is over threshold.
"""

import pytest

from tests_integration.support.helpers import (
    _assistant_transcript,
    assert_successful_smoke_session,
    execute_engine_prompt,
    prepare_smoke_session,
    require_real_engine,
    smoke_prompt,
)


pytestmark = pytest.mark.integration


# Engines with sandbox policies wired into workspace config. Keep in sync
# with .litehive/config.yaml engine_policies: each entry here must have a
# matching policy with extra_ro_binds (~/.codex, ~/.claude, etc.) and
# setenv HOME (so the engine can find its session state inside bwrap).
SANDBOXED_ENGINES = ("codex", "claude", "copilot", "gemini", "goz", "opencode")


@pytest.mark.parametrize("engine_name", SANDBOXED_ENGINES)
def test_engine_smoke_prompt_under_sandbox(engine_name: str, sandboxed_integration_root) -> None:
    """Each engine must return a successful reply to the smoke prompt when
    launched under the bwrap sandbox. Minimum guarantee: the per-engine
    policy (extra_ro_binds + HOME override + any CLI-specific env vars
    like CODEX_HOME) lets the engine read its own session state and
    authenticate with its provider from inside the sandbox."""
    require_real_engine(engine_name)
    engine, execution = execute_engine_prompt(
        engine_name,
        prompt=smoke_prompt(engine_name),
        cwd=sandboxed_integration_root,
        sandboxed=True,
    )
    assert execution.sandboxed, (
        f"expected sandbox wrapping for {engine_name}, got summary={execution.sandbox_summary!r}"
    )
    assert execution.exit_code == 0, (
        f"{engine_name} under sandbox failed with rc={execution.exit_code}\n"
        f"stderr_tail:\n{execution.stderr[-800:]}"
    )
    transcript = _assistant_transcript(engine.render_transcript(execution))
    assert transcript.strip(), (
        f"expected a non-empty assistant transcript from {engine_name} under sandbox"
    )


@pytest.mark.parametrize("engine_name", SANDBOXED_ENGINES)
def test_engine_smoke_session_sandboxed(engine_name: str, sandboxed_integration_root) -> None:
    """Run the existing smoke-session flow under sandbox: create a task,
    mark it active, run the engine against its smoke prompt. This is the
    closest proxy to a real pipeline subagent invocation."""
    require_real_engine(engine_name)
    session = prepare_smoke_session(engine_name, cwd=sandboxed_integration_root, sandboxed=True)
    assert_successful_smoke_session(session)
    assert session.execution.sandboxed, (
        f"expected sandboxed execution for {engine_name}, got {session.execution.sandbox_summary!r}"
    )
