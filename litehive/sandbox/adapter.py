"""External engine adapter wrapper that routes invocations through a sandbox launcher."""

from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from heru.base import CLIExecutionResult, CLIInvocation, ExternalCLIAdapter
from heru.engine_detection import (
    ORIGINAL_EXTERNAL_ADAPTER_RUN,
    ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE,
    filter_supported_kwargs,
    has_callable_override,
)

from litehive.agents.engine_callables import resolve_cli_execution_callable


@runtime_checkable
class SandboxSummary(Protocol):
    """Sandbox policy snapshot the adapter advertises to operator status surfaces."""

    @property
    def enabled(self) -> bool: ...

    @property
    def summary(self) -> str: ...


class SandboxLauncher(Protocol):
    """
    Sandbox-launcher contract used by ``SandboxedAdapter``.

    The adapter confines engine invocations without depending on a
    concrete launcher implementation; production passes the real
    ``SandboxLauncher`` in ``litehive.sandbox.launcher`` and tests can
    pass a stub that satisfies the protocol.
    """

    def policy_summary(self, engine_name: str) -> "SandboxSummary":
        """Resolve the effective sandbox policy snapshot for adapter status."""
        ...

    def wrap_invocation(
        self,
        engine_name: str,
        binary_name: str,
        invocation: CLIInvocation,
        role: str = "",
    ) -> CLIInvocation:
        """Rewrite a ``CLIInvocation`` to run inside the sandbox right before exec."""
        ...


class SandboxedAdapter(ExternalCLIAdapter):
    """
    Wrap a heru engine adapter so every invocation is finalized
    through the workspace sandbox launcher.

    Isolates "what command does the engine want to run" from "how do
    we confine that command on this host"; engine adapters stay
    unaware of sandboxing, and the sandbox does not have to know any
    engine's argv shape.
    """

    def __init__(self, adapter: ExternalCLIAdapter, launcher: SandboxLauncher, engine_name: str, role: str) -> None:
        """
        Wrap an existing engine adapter and snapshot its sandbox
        policy once at construction.

        Caching the policy snapshot avoids re-resolving config on
        every invocation, and keeps every subsequent ``run`` /
        ``run_live`` / status display agreeing on what the policy was
        at the time the adapter was built.
        """
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
        self._summary = launcher.policy_summary(engine_name)

    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        """Defer command construction to the wrapped engine adapter."""
        return self._adapter.build_command(
            prompt,
            cwd,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
        )

    def detect_capabilities(self):
        """Delegate capability detection to the wrapped adapter."""
        return self._adapter.detect_capabilities()

    def finalize_invocation(self, invocation: CLIInvocation) -> CLIInvocation:
        """Hand the invocation to the sandbox launcher right before exec."""
        return self._launcher.wrap_invocation(
            self._engine_name,
            self.binary,
            invocation,
            role=self._role,
        )

    def sandbox_details(self) -> tuple[bool, str]:
        """Expose the policy snapshot to the status/audit surface."""
        return (self._summary.enabled, self._summary.summary)

    def run(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        on_started: Callable[[int], None] | None = None,
        extra_env: dict[str, str] | None = None,
        emit_unified: bool = False,
    ) -> CLIExecutionResult:
        """
        Forward to the wrapped adapter's ``run`` when it overrides the
        base, while preserving sandbox finalization.
        """
        if has_callable_override(self._adapter, "run", ORIGINAL_EXTERNAL_ADAPTER_RUN):
            run_callable = resolve_cli_execution_callable(self._adapter, "run")
            run_kwargs: dict[str, object] = {"model": model}
            if max_turns is not None:
                run_kwargs["max_turns"] = max_turns
            if resume_session_id is not None:
                run_kwargs["resume_session_id"] = resume_session_id
            if on_started is not None:
                run_kwargs["on_started"] = on_started
            if extra_env is not None:
                run_kwargs["extra_env"] = extra_env
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
            extra_env=extra_env,
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
        on_started: Callable[[int], None] | None = None,
        on_update: Callable[[CLIExecutionResult], None] | None = None,
        inactivity_timeout_seconds: float = 0,
        extra_env: dict[str, str] | None = None,
        emit_unified: bool = False,
    ) -> CLIExecutionResult:
        """Streaming counterpart of ``run`` with the same sandbox-preserving override handling."""
        if has_callable_override(self._adapter, "run_live", ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE):
            run_live_callable = resolve_cli_execution_callable(self._adapter, "run_live")
            run_live_kwargs: dict[str, object] = {"model": model}
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
            if extra_env is not None:
                run_live_kwargs["extra_env"] = extra_env
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
            extra_env=extra_env,
            emit_unified=emit_unified,
        )

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        """Delegate transcript rendering to the wrapped adapter."""
        return self._adapter.render_transcript(execution)
