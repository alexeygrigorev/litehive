"""Shared adapter contract for fire-and-forget external CLIs."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import shutil
import subprocess
import json
import selectors
import time
from typing import Callable, Literal

from litehive.models import EngineUsageObservation, FollowUpTaskSpec, StageReport, SubagentStatus


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
    ) -> list[str]:
        raise NotImplementedError

    def build_invocation(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
    ) -> CLIInvocation:
        env = os.environ.copy()
        for key in self.stripped_env_vars:
            env.pop(key, None)
        return CLIInvocation(
            argv=tuple(self.build_command(prompt, cwd, model=model, max_turns=max_turns)),
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
        on_started: Callable[[int], None] | None = None,
    ) -> CLIExecutionResult:
        invocation = self.finalize_invocation(
            self.build_invocation(prompt, cwd, model=model, max_turns=max_turns)
        )
        sandboxed, sandbox_summary = self.sandbox_details()
        proc = subprocess.Popen(
            invocation.argv,
            cwd=str(invocation.cwd),
            env=invocation.env,
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
        on_started: Callable[[int], None] | None = None,
        on_update: Callable[[CLIExecutionResult], None] | None = None,
    ) -> CLIExecutionResult:
        invocation = self.finalize_invocation(
            self.build_invocation(prompt, cwd, model=model, max_turns=max_turns)
        )
        sandboxed, sandbox_summary = self.sandbox_details()
        proc = subprocess.Popen(
            invocation.argv,
            cwd=str(invocation.cwd),
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

        while selector.get_map():
            events = selector.select(timeout=self.LIVE_UPDATE_INTERVAL_SECONDS)
            if not events:
                if proc.poll() is None and time.monotonic() - last_update_at >= self.LIVE_UPDATE_INTERVAL_SECONDS:
                    emit_update()
                    last_update_at = time.monotonic()
                continue
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 4096)
                if chunk:
                    if key.data == "stdout":
                        stdout_chunks.extend(chunk)
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


def iter_jsonl_payloads(stdout: str) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
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


def parse_stage_report_text(
    *,
    task_id: str,
    step: Literal["grooming", "implementing", "testing", "accepting"],
    transcript: str,
    subagent_status: SubagentStatus,
) -> StageReport:
    follow_up_tasks, follow_up_warnings = _extract_follow_up_tasks(transcript)
    summary = _extract_line(transcript, "SUMMARY") or (
        transcript.splitlines()[0] if transcript else f"{step} completed"
    )
    return StageReport(
        task_id=task_id,
        step=step,
        verdict=_parse_verdict(transcript, subagent_status),  # type: ignore[arg-type]
        summary=summary,
        feedback=transcript,
        files_changed=_extract_list(transcript, "FILES_CHANGED"),
        follow_up_tasks=follow_up_tasks,
        tests={
            "added": _extract_int(transcript, "TESTS_ADDED"),
            "passing": _extract_int(transcript, "TESTS_PASSING"),
        },
        warnings=[*_extract_list(transcript, "WARNINGS"), *follow_up_warnings],
    )


def _extract_line(text: str, key: str) -> str | None:
    match = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _extract_int(text: str, key: str) -> int:
    value = _extract_line(text, key)
    if value is None:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def _extract_list(text: str, key: str) -> list[str]:
    items: list[str] = []
    capture = False
    for line in text.splitlines():
        if line.strip() == f"{key}:":
            capture = True
            continue
        if capture and re.match(r"^[A-Z_]+:", line):
            break
        if capture and line.lstrip().startswith("- "):
            items.append(line.split("- ", 1)[1].strip())
    return items


def _extract_follow_up_tasks(text: str) -> tuple[list[FollowUpTaskSpec], list[str]]:
    section = _extract_section_block(text, "FOLLOW_UP_TASKS")
    if section is None:
        return [], []
    try:
        payload = json.loads(section)
    except json.JSONDecodeError:
        return [], ["Ignoring invalid FOLLOW_UP_TASKS section: expected JSON array."]
    if not isinstance(payload, list):
        return [], ["Ignoring invalid FOLLOW_UP_TASKS section: expected JSON array."]

    follow_up_tasks: list[FollowUpTaskSpec] = []
    warnings: list[str] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            warnings.append(f"Ignoring invalid follow-up task #{index}: expected object.")
            continue
        title = item.get("title")
        rationale = item.get("rationale")
        if not isinstance(title, str) or not title.strip():
            warnings.append(f"Ignoring invalid follow-up task #{index}: missing title.")
            continue
        if not isinstance(rationale, str) or not rationale.strip():
            warnings.append(f"Ignoring invalid follow-up task #{index}: missing rationale.")
            continue
        acceptance_criteria = item.get("acceptance_criteria", [])
        if not isinstance(acceptance_criteria, list):
            warnings.append(
                f"Ignoring invalid follow-up task #{index}: acceptance_criteria must be a list."
            )
            continue
        task_type = item.get("task_type")
        if task_type is not None and not isinstance(task_type, str):
            warnings.append(f"Ignoring invalid follow-up task #{index}: task_type must be a string.")
            continue
        goal = item.get("goal", "")
        if not isinstance(goal, str):
            warnings.append(f"Ignoring invalid follow-up task #{index}: goal must be a string.")
            continue
        blocking = item.get("blocking", False)
        if not isinstance(blocking, bool):
            warnings.append(f"Ignoring invalid follow-up task #{index}: blocking must be true/false.")
            continue
        criteria = [entry.strip() for entry in acceptance_criteria if isinstance(entry, str) and entry.strip()]
        follow_up_tasks.append(
            FollowUpTaskSpec(
                title=title.strip(),
                rationale=rationale.strip(),
                blocking=blocking,
                goal=goal.strip(),
                acceptance_criteria=criteria,
                task_type=task_type.strip() if isinstance(task_type, str) else None,
            )
        )
    return follow_up_tasks, warnings


def _extract_section_block(text: str, key: str) -> str | None:
    capture = False
    lines: list[str] = []
    header = f"{key}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == header:
            capture = True
            continue
        if not capture and stripped.startswith(header):
            inline_value = stripped[len(header) :].strip()
            return inline_value or None
        if capture and re.match(r"^[A-Z_]+:", stripped):
            break
        if capture:
            lines.append(line)
    block = "\n".join(lines).strip()
    return block or None


def _parse_verdict(text: str, subagent_status: SubagentStatus) -> str:
    verdict = (_extract_line(text, "VERDICT") or "").strip().lower()
    mapping = {
        "pass": "pass",
        "accept": "accept",
        "fail": "fail",
        "reject": "reject",
        "blocked": "blocked",
    }
    if verdict in mapping:
        return mapping[verdict]
    return "pass" if subagent_status == "completed" else "blocked"
