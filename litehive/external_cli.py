"""Shared adapter contract for fire-and-forget external CLIs."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Literal

from litehive.models import StageReport, SubagentStatus


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

    def build_command(self, prompt: str, cwd: Path, model: str | None = None) -> list[str]:
        raise NotImplementedError

    def build_invocation(self, prompt: str, cwd: Path, model: str | None = None) -> CLIInvocation:
        env = os.environ.copy()
        for key in self.stripped_env_vars:
            env.pop(key, None)
        return CLIInvocation(
            argv=tuple(self.build_command(prompt, cwd, model=model)),
            cwd=cwd,
            env=env,
        )

    def run(self, prompt: str, cwd: Path, model: str | None = None) -> CLIExecutionResult:
        invocation = self.build_invocation(prompt, cwd, model=model)
        proc = subprocess.run(
            invocation.argv,
            cwd=str(invocation.cwd),
            capture_output=True,
            text=True,
            env=invocation.env,
            check=False,
        )
        return CLIExecutionResult(
            adapter=self.name,
            argv=invocation.argv,
            cwd=invocation.cwd,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

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
            transcript=execution.transcript,
            subagent_status=subagent_status,
        )


def parse_stage_report_text(
    *,
    task_id: str,
    step: Literal["grooming", "implementing", "testing", "accepting"],
    transcript: str,
    subagent_status: SubagentStatus,
) -> StageReport:
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
        tests={
            "added": _extract_int(transcript, "TESTS_ADDED"),
            "passing": _extract_int(transcript, "TESTS_PASSING"),
        },
        warnings=_extract_list(transcript, "WARNINGS"),
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
