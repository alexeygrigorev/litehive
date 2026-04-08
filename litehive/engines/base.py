"""Shared adapter contract for fire-and-forget external CLIs."""


from dataclasses import dataclass, replace
from collections import OrderedDict
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import json
import selectors
import time
from typing import Callable, Literal

logger = logging.getLogger(__name__)

from pydantic import ValidationError

from litehive.models import (
    EngineUsageObservation,
    LiveEvent,
    LiveTimeline,
    StageReport,
    StageResultSubmission,
    SubagentStatus,
    cap_feedback,
    utcnow,
)


TranscriptFormat = Literal["text", "jsonl"]


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    available: bool = False
    supports_model_override: bool = False
    strips_environment: bool = False
    transcript_format: TranscriptFormat = "text"


@dataclass(frozen=True, slots=True)
class CLIInvocation:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]


@dataclass(frozen=True, slots=True)
class CLIExecutionResult:
    adapter: str
    argv: tuple[str, ...]
    cwd: Path
    exit_code: int
    stdout: str
    stderr: str
    pid: int | None = None
    sandboxed: bool = False
    sandbox_summary: str = ""

    @property
    def returncode(self) -> int:
        return self.exit_code

    @property
    def transcript(self) -> str:
        parts = [self.stdout.strip()]
        if self.stderr.strip():
            parts.append(f"[stderr]\n{self.stderr.strip()}")
        return "\n\n".join(part for part in parts if part).strip()


@dataclass(frozen=True, slots=True)
class StreamEventAdapter:
    """Adapter hooks for JSONL event streams with partial live output."""

    unwrap_event: Callable[[dict[str, object]], dict[str, object]] | None = None
    text_deltas: Callable[[dict[str, object]], list[tuple[int, str]]] | None = None
    final_messages: Callable[[dict[str, object]], list[str]] | None = None
    errors: Callable[[dict[str, object]], list[str]] | None = None
    live_events: Callable[[dict[str, object]], list[LiveEvent]] | None = None

    def unwrap(self, payload: dict[str, object]) -> dict[str, object]:
        if self.unwrap_event is None:
            return payload
        return self.unwrap_event(payload)

    def extract_text_deltas(self, payload: dict[str, object]) -> list[tuple[int, str]]:
        if self.text_deltas is None:
            return []
        return self.text_deltas(payload)

    def extract_final_messages(self, payload: dict[str, object]) -> list[str]:
        if self.final_messages is None:
            return []
        return self.final_messages(payload)

    def extract_errors(self, payload: dict[str, object]) -> list[str]:
        if self.errors is None:
            return []
        return self.errors(payload)

    def extract_live_events(self, payload: dict[str, object]) -> list[LiveEvent]:
        if self.live_events is None:
            return []
        return self.live_events(payload)


class ExternalCLIAdapter:
    """Shared contract for one-shot external CLI adapters."""

    LIVE_UPDATE_INTERVAL_SECONDS = 0.5

    def __init__(
        self,
        *,
        name: str,
        binary: str,
        capabilities: AdapterCapabilities,
        stripped_env_vars: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.binary = binary
        self.capabilities = capabilities
        self.stripped_env_vars = stripped_env_vars

    def is_available(self) -> bool:
        return shutil.which(self.binary) is not None

    def detect_capabilities(self) -> AdapterCapabilities:
        return replace(self.capabilities, available=self.is_available())

    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def build_invocation(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> CLIInvocation:
        env = os.environ.copy()
        for key in self.stripped_env_vars:
            env.pop(key, None)
        return CLIInvocation(
            argv=tuple(self.build_command(
                prompt, cwd, model=model, max_turns=max_turns,
                resume_session_id=resume_session_id,
            )),
            cwd=cwd,
            env=env,
        )

    def finalize_invocation(self, invocation: CLIInvocation) -> CLIInvocation:
        return invocation

    def sandbox_details(self) -> tuple[bool, str]:
        return (False, "")

    def run(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        on_started: Callable[[int], None] | None = None,
    ) -> CLIExecutionResult:
        invocation = self.finalize_invocation(
            self.build_invocation(prompt, cwd, model=model, max_turns=max_turns,
                                  resume_session_id=resume_session_id)
        )
        sandboxed, sandbox_summary = self.sandbox_details()
        proc = subprocess.Popen(
            invocation.argv,
            cwd=str(invocation.cwd),
            env=invocation.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if on_started is not None:
            on_started(proc.pid)
        stdout, stderr = proc.communicate()
        return CLIExecutionResult(
            adapter=self.name,
            argv=invocation.argv,
            cwd=invocation.cwd,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            pid=proc.pid,
            sandboxed=sandboxed,
            sandbox_summary=sandbox_summary,
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
    ) -> CLIExecutionResult:
        invocation = self.finalize_invocation(
            self.build_invocation(prompt, cwd, model=model, max_turns=max_turns,
                                  resume_session_id=resume_session_id)
        )
        sandboxed, sandbox_summary = self.sandbox_details()
        proc = subprocess.Popen(
            invocation.argv,
            cwd=str(invocation.cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=invocation.env,
            text=False,
        )
        if on_started is not None:
            on_started(proc.pid)
        assert proc.stdout is not None
        assert proc.stderr is not None

        stdout_chunks = bytearray()
        stderr_chunks = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, data="stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, data="stderr")
        last_update_at = time.monotonic()

        def drain_after_abort() -> None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    stdout_tail, stderr_tail = proc.communicate(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout_tail, stderr_tail = proc.communicate()
            else:
                stdout_tail, stderr_tail = proc.communicate()
            stdout_chunks.extend(stdout_tail or b"")
            stderr_chunks.extend(stderr_tail or b"")

        def emit_update() -> None:
            if on_update is None:
                return
            on_update(
                CLIExecutionResult(
                    adapter=self.name,
                    argv=invocation.argv,
                    cwd=invocation.cwd,
                    exit_code=proc.poll() or 0,
                    stdout=stdout_chunks.decode("utf-8", errors="replace"),
                    stderr=stderr_chunks.decode("utf-8", errors="replace"),
                    pid=proc.pid,
                    sandboxed=sandboxed,
                    sandbox_summary=sandbox_summary,
                )
            )

        last_output_at = time.monotonic()

        try:
            while selector.get_map():
                events = selector.select(timeout=self.LIVE_UPDATE_INTERVAL_SECONDS)

                # Always check inactivity timeout, even when data is flowing
                if (
                    inactivity_timeout_seconds > 0
                    and proc.poll() is None
                    and time.monotonic() - last_output_at > inactivity_timeout_seconds
                ):
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    stderr_chunks.extend(
                        f"\n[litehive] Process killed after {inactivity_timeout_seconds:.0f}s of inactivity.\n".encode()
                    )
                    break

                if not events:
                    if (
                        proc.poll() is None
                        and time.monotonic() - last_update_at >= self.LIVE_UPDATE_INTERVAL_SECONDS
                    ):
                        emit_update()
                        last_update_at = time.monotonic()
                    continue
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if chunk:
                        if key.data == "stdout":
                            stdout_chunks.extend(chunk)
                            last_output_at = time.monotonic()
                        else:
                            stderr_chunks.extend(chunk)
                        emit_update()
                        last_update_at = time.monotonic()
                        continue
                    selector.unregister(key.fileobj)
                    key.fileobj.close()

            exit_code = proc.wait()
            result = CLIExecutionResult(
                adapter=self.name,
                argv=invocation.argv,
                cwd=invocation.cwd,
                exit_code=exit_code,
                stdout=stdout_chunks.decode("utf-8", errors="replace"),
                stderr=stderr_chunks.decode("utf-8", errors="replace"),
                pid=proc.pid,
                sandboxed=sandboxed,
                sandbox_summary=sandbox_summary,
            )
            if on_update is not None:
                on_update(result)
            return result
        except BaseException:
            drain_after_abort()
            raise
        finally:
            selector.close()

    def parse_stage_report(
        self,
        *,
        task_id: str,
        step: Literal["grooming", "implementing", "testing", "accepting"],
        execution: CLIExecutionResult,
        subagent_status: SubagentStatus,
    ) -> StageReport:
        return parse_stage_report_text(
            task_id=task_id,
            step=step,
            transcript=self.render_transcript(execution),
            subagent_status=subagent_status,
        )

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        return execution.transcript

    def extract_usage_observation(
        self,
        execution: CLIExecutionResult,
    ) -> EngineUsageObservation | None:
        return None


def extract_jsonl_messages(stdout: str) -> str:
    parts: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        content: str | None = None
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            raw_content = payload.get("content")
            if isinstance(raw_content, str) and raw_content:
                content = raw_content
        elif payload.get("type") == "assistant.message":
            data = payload.get("data")
            if isinstance(data, dict):
                raw_content = data.get("content")
                if isinstance(raw_content, str) and raw_content:
                    content = raw_content
        if content is None:
            continue
        parts.append(content)
    return "".join(parts).strip()


def extract_stream_transcript(
    stdout: str,
    *,
    adapter: StreamEventAdapter,
    delta_fallback: Callable[[str], list[str]] | None = None,
) -> str:
    messages: list[str] = []
    partial_blocks: dict[int, list[str]] = {}
    for raw_payload in iter_jsonl_payloads(stdout):
        payload = adapter.unwrap(raw_payload)
        for message in adapter.extract_final_messages(payload):
            if message:
                messages.append(message)
        for index, text in adapter.extract_text_deltas(payload):
            if not text:
                continue
            partial_blocks.setdefault(index, []).append(text)
    if not messages and partial_blocks:
        messages.extend("".join(partial_blocks[index]) for index in sorted(partial_blocks))
    if not messages and delta_fallback is not None:
        messages.extend(text for text in delta_fallback(stdout) if text)
    return "\n".join(messages).strip()


def extract_stream_errors(stdout: str, *, adapter: StreamEventAdapter) -> list[str]:
    errors: list[str] = []
    for raw_payload in iter_jsonl_payloads(stdout):
        payload = adapter.unwrap(raw_payload)
        errors.extend(error for error in adapter.extract_errors(payload) if error)
    return errors


def iter_jsonl_payloads(stdout: str) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(stdout.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "iter_jsonl_payloads: skipping unparseable line %d: %s (content: %.200s)",
                line_number,
                exc,
                line,
            )
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
        else:
            logger.warning(
                "iter_jsonl_payloads: skipping non-object JSON at line %d (type=%s, content: %.200s)",
                line_number,
                type(payload).__name__,
                line,
            )
    return payloads


def extract_jsonl_errors(stdout: str) -> list[str]:
    errors: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        if payload.get("type") == "tool_result" and payload.get("status") == "error":
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                errors.append(error["message"])
            continue

        if payload.get("type") == "error":
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                errors.append(data["message"])
    return errors


def extract_codex_messages(stdout: str) -> str:
    messages: OrderedDict[str, str] = OrderedDict()
    for payload in iter_jsonl_payloads(stdout):
        if payload.get("type") not in {"item.completed", "item.updated"}:
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            messages[item_id] = text
    return "\n".join(messages.values()).strip()


def extract_codex_errors(stdout: str) -> list[str]:
    errors: list[str] = []
    command_errors: OrderedDict[str, str] = OrderedDict()
    for payload in iter_jsonl_payloads(stdout):
        event_type = payload.get("type")
        if event_type in {"error", "turn.failed"}:
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                errors.append(message.strip())
            continue

        if event_type not in {"item.completed", "item.updated"}:
            continue
        item = payload.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "command_execution":
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            continue
        aggregated_output = item.get("aggregated_output")
        if isinstance(aggregated_output, str) and aggregated_output.strip():
            if item.get("status") == "failed" or item.get("exit_code") not in {None, 0}:
                command_errors[item_id] = aggregated_output.strip()
                continue
        command_errors.pop(item_id, None)
    return [*errors, *command_errors.values()]


def parse_stage_report_text(
    *,
    task_id: str,
    step: Literal["grooming", "implementing", "testing", "accepting"],
    transcript: str,
    subagent_status: SubagentStatus,
) -> StageReport:
    # Prefer schema-validated structured submission when present.
    submission = _extract_stage_result_submission(transcript)
    if isinstance(submission, StageResultSubmission):
        task_update_dict: dict[str, object] = (
            submission.task_update.model_dump(exclude_unset=True) if submission.task_update else {}
        )
        if submission.acceptance_criteria and "acceptance_criteria" not in task_update_dict:
            task_update_dict["acceptance_criteria"] = submission.acceptance_criteria
        return StageReport(
            task_id=task_id,
            step=step,
            verdict=submission.verdict,  # type: ignore[arg-type]
            summary=submission.summary,
            feedback=cap_feedback(transcript),
            follow_up_tasks=submission.follow_up_tasks,
            task_update=task_update_dict,
            tests={"added": submission.tests.added, "passing": submission.tests.passing},
            warnings=submission.warnings,
        )

    # No valid structured submission and no CLI verdict — verdict is fail.
    # Attach validation warnings if the structured block was present but invalid.
    warnings: list[str] = ["Agent did not submit verdict via litehive report CLI."]
    if isinstance(submission, ValidationError):
        warnings.extend(_format_stage_result_validation_errors(submission))

    summary = (
        transcript.splitlines()[0] if transcript else f"{step} completed without verdict"
    )
    return StageReport(
        task_id=task_id,
        step=step,
        verdict="fail",
        summary=summary,
        feedback=cap_feedback(transcript),
        warnings=warnings,
    )






def _extract_stage_result_submission(
    text: str,
) -> StageResultSubmission | ValidationError | None:
    """Extract and validate a ``STAGE_RESULT:`` JSON block from transcript text.

    Returns the validated model on success, a ``ValidationError`` when the JSON
    is present but invalid, or ``None`` when no ``STAGE_RESULT:`` block exists.
    """
    # Inline extraction of the STAGE_RESULT: section from text.
    capture = False
    section_lines: list[str] = []
    header = "STAGE_RESULT:"
    section: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == header:
            capture = True
            continue
        if not capture and stripped.startswith(header):
            inline_value = stripped[len(header):].strip()
            section = inline_value or None
            break
        if capture and re.match(r"^[A-Z_]+:", stripped):
            break
        if capture:
            section_lines.append(line)
    if section is None and section_lines:
        section = "\n".join(section_lines).rstrip() or None
    if section is None:
        return None
    try:
        payload = json.loads(section)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    # Normalise verdict to lowercase before validation.
    verdict = payload.get("verdict")
    if isinstance(verdict, str):
        payload["verdict"] = verdict.strip().lower()
    try:
        return StageResultSubmission.model_validate(payload)
    except ValidationError as exc:
        return exc


def _format_stage_result_validation_errors(exc: ValidationError) -> list[str]:
    """Convert a Pydantic ``ValidationError`` into human-readable warning lines."""
    warnings: list[str] = []
    for error in exc.errors():
        loc = " -> ".join(str(part) for part in error["loc"]) if error["loc"] else "(root)"
        warnings.append(f"STAGE_RESULT validation: {loc}: {error['msg']}")
    return warnings




def extract_live_timeline(
    stdout: str,
    *,
    engine: str,
    adapter: StreamEventAdapter | None = None,
) -> LiveTimeline:
    events: list[LiveEvent] = []
    sequence = 0
    for raw_payload in iter_jsonl_payloads(stdout):
        payload = adapter.unwrap(raw_payload) if adapter is not None else raw_payload
        if adapter is not None:
            for event in adapter.extract_live_events(payload):
                events.append(
                    LiveEvent(
                        kind=event.kind,
                        engine=event.engine or engine,
                        sequence=sequence,
                        timestamp=event.timestamp or utcnow(),
                        role=event.role,
                        content=event.content,
                        tool_name=event.tool_name,
                        tool_input=event.tool_input,
                        tool_output=event.tool_output,
                        error=event.error,
                        metadata=event.metadata,
                    )
                )
                sequence += 1
            continue
        for message in _generic_final_messages(payload):
            events.append(
                LiveEvent(
                    kind="message",
                    engine=engine,
                    sequence=sequence,
                    role="assistant",
                    content=message,
                )
            )
            sequence += 1
        for error in _generic_errors(payload):
            events.append(
                LiveEvent(
                    kind="error",
                    engine=engine,
                    sequence=sequence,
                    error=error,
                )
            )
            sequence += 1
    timeline = LiveTimeline(engine=engine)
    timeline.events = events
    timeline.recompute_counts()
    return timeline


def _generic_final_messages(payload: dict[str, object]) -> list[str]:
    content: str | None = None
    if payload.get("type") == "message" and payload.get("role") == "assistant":
        raw_content = payload.get("content")
        if isinstance(raw_content, str) and raw_content:
            content = raw_content
    elif payload.get("type") == "assistant.message":
        data = payload.get("data")
        if isinstance(data, dict):
            raw_content = data.get("content")
            if isinstance(raw_content, str) and raw_content:
                content = raw_content
    elif payload.get("type") == "item.completed":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text:
                content = text
    if content is None:
        return []
    return [content]


def _generic_errors(payload: dict[str, object]) -> list[str]:
    if payload.get("type") == "error":
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            return [data["message"]]
        message = payload.get("message")
        if isinstance(message, str) and message:
            return [message]
    return []
