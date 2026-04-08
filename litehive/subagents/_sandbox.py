"""Sandboxed adapter wrapper for subagent execution."""

from pathlib import Path

from litehive.engines.base import CLIExecutionResult, ExternalCLIAdapter
from litehive.engines.sandbox import SandboxLauncher

from litehive.subagents._engine_detection import (
    _ORIGINAL_EXTERNAL_ADAPTER_RUN,
    _ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE,
    _has_callable_override,
)


class _SandboxedAdapter(ExternalCLIAdapter):
    def __init__(
        self, adapter: ExternalCLIAdapter, launcher: SandboxLauncher, engine_name: str
    ) -> None:
        super().__init__(
            name=adapter.name,
            binary=adapter.binary,
            capabilities=adapter.capabilities,
            stripped_env_vars=adapter.stripped_env_vars,
        )
        self._adapter = adapter
        self._launcher = launcher
        self._engine_name = engine_name
        self._summary = launcher.policy_summary(engine_name)

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
        return self._launcher.wrap_invocation(self._engine_name, self.binary, invocation)

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
    ) -> CLIExecutionResult:
        if _has_callable_override(self._adapter, "run", _ORIGINAL_EXTERNAL_ADAPTER_RUN):
            return self._adapter.run(
                prompt,
                cwd,
                model=model,
                max_turns=max_turns,
                resume_session_id=resume_session_id,
                on_started=on_started,
            )
        return super().run(
            prompt,
            cwd,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
            on_started=on_started,
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
    ) -> CLIExecutionResult:
        if _has_callable_override(self._adapter, "run_live", _ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE):
            return self._adapter.run_live(
                prompt,
                cwd,
                model=model,
                max_turns=max_turns,
                resume_session_id=resume_session_id,
                on_started=on_started,
                on_update=on_update,
                inactivity_timeout_seconds=inactivity_timeout_seconds,
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
        )

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        return self._adapter.render_transcript(execution)

    def parse_stage_report(
        self,
        *,
        task_id: str,
        step: str,
        execution: CLIExecutionResult,
        subagent_status: str,
    ):
        return self._adapter.parse_stage_report(
            task_id=task_id,
            step=step,
            execution=execution,
            subagent_status=subagent_status,
        )
