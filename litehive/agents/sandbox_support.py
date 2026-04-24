"""Generic sandbox helpers for wrapping external engine adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Protocol

from heru.base import CLIExecutionResult, ExternalCLIAdapter
from heru.engine_detection import (
    ORIGINAL_EXTERNAL_ADAPTER_RUN,
    ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE,
    effective_engine_callable,
    filter_supported_kwargs,
    has_callable_override,
)


class SandboxSummary(Protocol):
    enabled: bool
    summary: str


class SandboxLauncher(Protocol):
    def policy_summary(self, engine_name: str, role: str = "") -> SandboxSummary: ...

    def wrap_invocation(
        self,
        engine_name: str,
        binary_name: str,
        invocation: object,
        role: str = "",
    ) -> object: ...


def forced_engine_rw_state_dirs(
    engine_name: str,
    policy: object | None,
    env: Mapping[str, str] | None = None,
) -> frozenset[Path]:
    """State dirs that an engine must be able to write into."""

    effective_env = dict(env or {})
    setenv = getattr(policy, "setenv", None)
    if isinstance(setenv, dict):
        effective_env.update(setenv)
    home_override = effective_env.get("HOME")
    home = Path(home_override).expanduser() if home_override else Path.home()

    candidates: list[Path] = []
    if engine_name == "codex":
        codex_home = effective_env.get("CODEX_HOME")
        candidates.append(Path(codex_home).expanduser() if codex_home else home / ".codex")
    elif engine_name == "claude":
        candidates.append(home / ".claude")
    elif engine_name == "copilot":
        candidates.append(home / ".copilot")
    elif engine_name == "gemini":
        candidates.append(home / ".gemini")
    elif engine_name == "opencode":
        candidates.append(home / ".config" / "opencode")
    elif engine_name == "goz":
        candidates.append(home / ".goz")
        candidates.append(home / ".config" / "goz")

    resolved: set[Path] = set()
    for candidate in candidates:
        try:
            resolved.add(candidate.resolve())
        except OSError:
            continue
    return frozenset(resolved)


def sanitize_path_env(raw_path: str) -> str:
    """Drop PATH segments that point at ephemeral codex arg0 dirs."""

    if not raw_path:
        return raw_path
    kept: list[str] = []
    for segment in raw_path.split(":"):
        if not segment:
            continue
        if "codex-arg0" in segment:
            continue
        if "codex-linux-" in segment and segment.endswith("/path"):
            continue
        kept.append(segment)
    return ":".join(kept)


class SandboxedAdapter(ExternalCLIAdapter):
    def __init__(self, adapter: ExternalCLIAdapter, launcher: SandboxLauncher, engine_name: str, role: str) -> None:
        super().__init__(
            name=adapter.name,
            binary=adapter.binary,
            capabilities=adapter.capabilities,
            stripped_env_vars=adapter.stripped_env_vars,
        )
        self._adapter = adapter
        self._launcher = launcher
        self._engine_name = engine_name
        self._role = role
        self._summary = launcher.policy_summary(engine_name, role)

    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        return self._adapter.build_command(
            prompt,
            cwd,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
        )

    def detect_capabilities(self):
        return self._adapter.detect_capabilities()

    def finalize_invocation(self, invocation):
        return self._launcher.wrap_invocation(
            self._engine_name,
            self.binary,
            invocation,
            role=self._role,
        )

    def sandbox_details(self) -> tuple[bool, str]:
        return (self._summary.enabled, self._summary.summary)

    def run(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        on_started=None,
        emit_unified: bool = False,
    ) -> CLIExecutionResult:
        if has_callable_override(self._adapter, "run", ORIGINAL_EXTERNAL_ADAPTER_RUN):
            run_callable = effective_engine_callable(self._adapter, "run")
            if not callable(run_callable):
                run_callable = self._adapter.run
            run_kwargs = {"model": model}
            if max_turns is not None:
                run_kwargs["max_turns"] = max_turns
            if resume_session_id is not None:
                run_kwargs["resume_session_id"] = resume_session_id
            if on_started is not None:
                run_kwargs["on_started"] = on_started
            run_kwargs["emit_unified"] = emit_unified
            return run_callable(
                prompt,
                cwd,
                **filter_supported_kwargs(run_callable, run_kwargs),
            )
        return super().run(
            prompt,
            cwd,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
            on_started=on_started,
            emit_unified=emit_unified,
        )

    def run_live(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        on_started=None,
        on_update=None,
        inactivity_timeout_seconds: float = 0,
        emit_unified: bool = False,
    ) -> CLIExecutionResult:
        if has_callable_override(self._adapter, "run_live", ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE):
            run_live_callable = effective_engine_callable(self._adapter, "run_live")
            if not callable(run_live_callable):
                run_live_callable = self._adapter.run_live
            run_live_kwargs = {"model": model}
            if max_turns is not None:
                run_live_kwargs["max_turns"] = max_turns
            if resume_session_id is not None:
                run_live_kwargs["resume_session_id"] = resume_session_id
            if on_started is not None:
                run_live_kwargs["on_started"] = on_started
            if on_update is not None:
                run_live_kwargs["on_update"] = on_update
            if inactivity_timeout_seconds > 0:
                run_live_kwargs["inactivity_timeout_seconds"] = inactivity_timeout_seconds
            run_live_kwargs["emit_unified"] = emit_unified
            return run_live_callable(
                prompt,
                cwd,
                **filter_supported_kwargs(run_live_callable, run_live_kwargs),
            )
        return super().run_live(
            prompt,
            cwd,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
            on_started=on_started,
            on_update=on_update,
            inactivity_timeout_seconds=inactivity_timeout_seconds,
            emit_unified=emit_unified,
        )

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        return self._adapter.render_transcript(execution)


__all__ = [
    "SandboxedAdapter",
    "forced_engine_rw_state_dirs",
    "sanitize_path_env",
]
