"""Subagent execution and folder persistence."""

from __future__ import annotations

from dataclasses import dataclass
import gzip
import inspect
import os
from pathlib import Path
import re

import yaml

from litehive.config import load_config, resolve_process_profile
from litehive.external_cli import CLIExecutionResult, ExternalCLIAdapter, parse_stage_report_text
from litehive.engines import (
    EngineError,
    classify_execution_limit,
    classify_retryable_execution_failure,
    get_engine,
)
from litehive.models import ResourceLimitEvent, StageReport, SubagentRef, TaskRecord, utcnow
from litehive.sandbox import SandboxError, SandboxLauncher
from litehive.tasks import (
    _write_atomic_files,
    infer_acceptance_criteria,
    mark_subagent_finished,
    mark_subagent_pid,
    mark_subagent_started,
    missing_acceptance_criteria_reason,
    save_task,
    task_dir,
    task_template,
)


@dataclass(slots=True)
class EngineFailure:
    kind: str
    reason: str
    classification: str | None = None
    resource_limit_event: ResourceLimitEvent | None = None


@dataclass(slots=True)
class SubagentResult:
    ref: SubagentRef
    execution: CLIExecutionResult | None
    transcript: str
    exit_code: int
    failure: EngineFailure | None = None


_COMPRESS_STREAM_ARTIFACT_MIN_BYTES = 4096


def _write_atomic_gzip_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with gzip.open(temp_path, "wt", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_stream_artifact(base: Path, name: str, content: str, *, compress: bool) -> None:
    plain_path = base / f"{name}.txt"
    compressed_path = base / f"{name}.txt.gz"
    if compress and not content:
        if plain_path.exists():
            plain_path.unlink()
        if compressed_path.exists():
            compressed_path.unlink()
        return
    should_compress = compress and len(content.encode("utf-8")) >= _COMPRESS_STREAM_ARTIFACT_MIN_BYTES
    if should_compress:
        if plain_path.exists():
            plain_path.unlink()
        _write_atomic_gzip_text(compressed_path, content)
        return
    if compressed_path.exists():
        compressed_path.unlink()
    _write_atomic_files({plain_path: content})


def _supports_live_execution(engine: object) -> bool:
    run_live = getattr(engine, "run_live", None)
    if not callable(run_live):
        return False
    return not _prefers_non_live_run(engine)


def _prefers_non_live_run(engine: object) -> bool:
    engine_dict = getattr(engine, "__dict__", {})
    if "run" in engine_dict and "run_live" not in engine_dict:
        return True

    engine_type = type(engine)
    run_impl = getattr(engine_type, "run", None)
    run_live_impl = getattr(engine_type, "run_live", None)
    return (
        run_impl is not ExternalCLIAdapter.run
        and run_live_impl is ExternalCLIAdapter.run_live
    )


def _supports_on_started(engine: object) -> bool:
    run = getattr(engine, "run", None)
    if not callable(run):
        return False
    try:
        return "on_started" in inspect.signature(run).parameters
    except (TypeError, ValueError):
        return False


def _supports_live_on_started(engine: object) -> bool:
    run_live = getattr(engine, "run_live", None)
    if not callable(run_live):
        return False
    try:
        return "on_started" in inspect.signature(run_live).parameters
    except (TypeError, ValueError):
        return False


class SubagentManager:
    """Run external CLI subagents inside a task-scoped folder."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.config = load_config(self.root)
        self.sandbox = SandboxLauncher(self.root, self.config)

    def run(
        self,
        task: TaskRecord,
        *,
        role: str,
        engine_name: str,
        prompt: str,
        model: str | None = None,
        max_turns: int | None = None,
    ) -> SubagentResult:
        subagent_id = self._next_subagent_id(task)
        folder_name = f"{subagent_id}-{role}"
        base = task_dir(self.root, task) / "subagents" / folder_name
        base.mkdir(parents=True, exist_ok=False)

        engine = get_engine(engine_name)
        sandbox_summary = self.sandbox.policy_summary(engine_name)
        ref = SubagentRef(
            id=subagent_id,
            role=role,
            engine=engine_name,
            status="running",
            path=f"subagents/{folder_name}",
            sandboxed=sandbox_summary.enabled,
            sandbox_summary=sandbox_summary.summary,
        )
        task.subagents.append(ref)
        save_task(self.root, task)
        mark_subagent_started(self.root, task, ref)
        self._write_session_start(base, ref, prompt)
        failure: EngineFailure | None = None
        try:
            if not engine.is_available():
                raise EngineError(f"Engine '{engine.name}' is unavailable: missing binary '{engine.binary}'")
            if isinstance(engine, ExternalCLIAdapter) and sandbox_summary.enabled:
                engine = _SandboxedAdapter(engine, self.sandbox, engine_name)
            if _supports_live_execution(engine):
                live_kwargs: dict[str, object] = {
                    "cwd": self.root,
                    "model": model,
                    "on_update": lambda execution: self._write_session_progress(
                        task,
                        base,
                        ref,
                        prompt,
                        execution,
                    ),
                }
                if _supports_live_on_started(engine):
                    live_kwargs["on_started"] = lambda pid: self._record_subagent_pid(task, base, ref, pid)
                if max_turns is None:
                    proc = engine.run_live(prompt, **live_kwargs)
                else:
                    proc = engine.run_live(prompt, max_turns=max_turns, **live_kwargs)
            elif max_turns is None:
                if _supports_on_started(engine):
                    proc = engine.run(
                        prompt,
                        cwd=self.root,
                        model=model,
                        on_started=lambda pid: self._record_subagent_pid(task, base, ref, pid),
                    )
                else:
                    proc = engine.run(prompt, cwd=self.root, model=model)
            else:
                if _supports_on_started(engine):
                    proc = engine.run(
                        prompt,
                        cwd=self.root,
                        model=model,
                        max_turns=max_turns,
                        on_started=lambda pid: self._record_subagent_pid(task, base, ref, pid),
                    )
                else:
                    proc = engine.run(prompt, cwd=self.root, model=model, max_turns=max_turns)
            transcript = engine.render_transcript(proc)
            ref.status = "completed" if proc.exit_code == 0 else "failed"
            if proc.exit_code != 0:
                resource_limit_event = self.sandbox.classify_resource_limit_event(
                    engine_name,
                    exit_code=proc.exit_code,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
                if resource_limit_event is not None:
                    failure = EngineFailure(
                        kind="resource_limit",
                        reason=resource_limit_event.reason,
                        classification=resource_limit_event.resource,
                        resource_limit_event=resource_limit_event,
                    )
                else:
                    limit_reason = classify_execution_limit(transcript)
                    if limit_reason is not None:
                        failure = EngineFailure(kind="execution_limit", reason=limit_reason)
                    else:
                        retryable_failure = classify_retryable_execution_failure(transcript)
                        if retryable_failure is not None:
                            failure = EngineFailure(
                                kind="retryable_execution_error",
                                reason=retryable_failure.reason,
                                classification=retryable_failure.classification,
                            )
        except (EngineError, SandboxError) as exc:
            transcript = str(exc)
            proc = None
            ref.status = "blocked"
            failure = EngineFailure(kind="engine_error", reason=str(exc))

        save_task(self.root, task)
        mark_subagent_finished(
            self.root,
            task,
            ref,
            transcript,
            0 if proc is None else proc.exit_code,
            pid=None if proc is None else proc.pid,
            resource_limit_event=None if failure is None else failure.resource_limit_event,
        )
        self._write_session_finish(
            task,
            base,
            ref,
            prompt,
            transcript,
            0 if proc is None else proc.exit_code,
            proc,
            resource_limit_event=None if failure is None else failure.resource_limit_event,
        )
        return SubagentResult(
            ref=ref,
            execution=proc,
            transcript=transcript,
            exit_code=0 if proc is None else proc.exit_code,
            failure=failure,
        )

    def _next_subagent_id(self, task: TaskRecord) -> str:
        next_number = 1
        for ref in task.subagents:
            match = re.match(r"^SA-(\d{4})$", ref.id)
            if match:
                next_number = max(next_number, int(match.group(1)) + 1)

        subagents_root = task_dir(self.root, task) / "subagents"
        if subagents_root.exists():
            for child in subagents_root.iterdir():
                if not child.is_dir():
                    continue
                match = re.match(r"^SA-(\d{4})-", child.name)
                if match:
                    next_number = max(next_number, int(match.group(1)) + 1)

        return f"SA-{next_number:04d}"

    def _write_session_start(
        self,
        base: Path,
        ref: SubagentRef,
        prompt: str,
    ) -> None:
        self._write_session_snapshot(
            base,
            ref,
            prompt=prompt,
            transcript="",
            stdout="",
            stderr="",
            report_payload={
                "status": ref.status,
                "summary": "",
                "files_changed": [],
                "tests": {"added": 0, "passing": 0},
                "warnings": [],
                "resource_control": self.sandbox.policy_summary(ref.engine).as_dict(),
                "resource_limit_event": None,
            },
            exit_code=None,
            pid=None,
            resource_limit_event=None,
        )

    def _write_session_finish(
        self,
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
        prompt: str,
        transcript: str,
        exit_code: int,
        execution: CLIExecutionResult | None,
        resource_limit_event: ResourceLimitEvent | None,
    ) -> None:
        report_step = (
            task.pipeline_status
            if task.pipeline_status in {"grooming", "implementing", "testing", "accepting", "commit_to_git"}
            else "implementing"
        )
        if resource_limit_event is not None:
            report = StageReport(
                task_id=task.id,
                step=report_step,  # type: ignore[arg-type]
                verdict="blocked",
                summary=f"{report_step} blocked: {resource_limit_event.reason}",
                feedback=transcript,
                warnings=[resource_limit_event.reason],
                resource_limit_event=resource_limit_event,
            )
        else:
            report = parse_stage_report_text(
                task_id=task.id,
                step=report_step,  # type: ignore[arg-type]
                transcript=transcript,
                subagent_status=ref.status,
            )
        self._write_session_snapshot(
            base,
            ref,
            prompt=prompt,
            transcript=transcript + "\n",
            stdout="" if execution is None else execution.stdout,
            stderr="" if execution is None else execution.stderr,
            report_payload={
                "status": ref.status,
                "summary": report.summary,
                "files_changed": report.files_changed,
                "tests": report.tests,
                "warnings": report.warnings,
                "resource_control": self.sandbox.policy_summary(ref.engine).as_dict(),
                "resource_limit_event": (
                    None
                    if report.resource_limit_event is None
                    else report.resource_limit_event.model_dump(mode="python")
                ),
            },
            exit_code=exit_code,
            pid=None if execution is None else execution.pid,
            resource_limit_event=resource_limit_event,
        )
        _write_stream_artifact(base, "stdout", "" if execution is None else execution.stdout, compress=True)
        _write_stream_artifact(base, "stderr", "" if execution is None else execution.stderr, compress=True)

    def _write_session_progress(
        self,
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
        prompt: str,
        execution: CLIExecutionResult,
    ) -> None:
        transcript = get_engine(ref.engine).render_transcript(execution)
        self._record_subagent_pid(task, base, ref, execution.pid)
        report_step = (
            task.pipeline_status
            if task.pipeline_status in {"grooming", "implementing", "testing", "accepting", "commit_to_git"}
            else "implementing"
        )
        report_payload = {
            "status": ref.status,
            "summary": "",
            "files_changed": [],
            "tests": {"added": 0, "passing": 0},
            "warnings": [],
            "resource_control": self.sandbox.policy_summary(ref.engine).as_dict(),
            "resource_limit_event": None,
        }
        if transcript.strip():
            report = parse_stage_report_text(
                task_id=task.id,
                step=report_step,  # type: ignore[arg-type]
                transcript=transcript,
                subagent_status=ref.status,
            )
            report_payload = {
                "status": ref.status,
                "summary": report.summary,
                "files_changed": report.files_changed,
                "tests": report.tests,
                "warnings": report.warnings,
                "resource_control": self.sandbox.policy_summary(ref.engine).as_dict(),
                "resource_limit_event": (
                    None
                    if report.resource_limit_event is None
                    else report.resource_limit_event.model_dump(mode="python")
                ),
            }
        self._write_session_snapshot(
            base,
            ref,
            prompt=prompt,
            transcript=transcript,
            stdout=execution.stdout,
            stderr=execution.stderr,
            report_payload=report_payload,
            exit_code=None,
            pid=execution.pid,
            resource_limit_event=None,
        )

    def _write_session_metadata(
        self,
        base: Path,
        ref: SubagentRef,
        *,
        exit_code: int | None,
        pid: int | None,
        resource_limit_event: ResourceLimitEvent | None = None,
    ) -> None:
        created_at = utcnow()
        session_path = base / "session.yaml"
        resource_control = self.sandbox.policy_summary(ref.engine).as_dict()
        if session_path.exists():
            existing = yaml.safe_load(session_path.read_text(encoding="utf-8")) or {}
            if isinstance(existing, dict) and isinstance(existing.get("created_at"), str):
                created_at = existing["created_at"]
        _write_atomic_files(
            {
                session_path: yaml.safe_dump(
                    {
                        "id": ref.id,
                        "role": ref.role,
                        "engine": ref.engine,
                        "status": ref.status,
                        "sandboxed": ref.sandboxed,
                        "sandbox": ref.sandbox_summary or "host",
                        "created_at": created_at,
                        "updated_at": utcnow(),
                        "pid": pid,
                        "exit_code": exit_code,
                        "resource_control": resource_control,
                        "resource_limit_event": (
                            None
                            if resource_limit_event is None
                            else resource_limit_event.model_dump(mode="python")
                        ),
                    },
                    sort_keys=False,
                )
            }
        )

    def _record_subagent_pid(self, task: TaskRecord, base: Path, ref: SubagentRef, pid: int | None) -> None:
        if pid is None:
            return
        mark_subagent_pid(self.root, task, pid)
        self._write_session_metadata(base, ref, exit_code=None, pid=pid, resource_limit_event=None)

    def _write_session_snapshot(
        self,
        base: Path,
        ref: SubagentRef,
        *,
        prompt: str,
        transcript: str,
        stdout: str,
        stderr: str,
        report_payload: dict[str, object],
        exit_code: int | None,
        pid: int | None,
        resource_limit_event: ResourceLimitEvent | None,
    ) -> None:
        created_at = utcnow()
        session_path = base / "session.yaml"
        resource_control = self.sandbox.policy_summary(ref.engine).as_dict()
        if session_path.exists():
            existing = yaml.safe_load(session_path.read_text(encoding="utf-8")) or {}
            if isinstance(existing, dict) and isinstance(existing.get("created_at"), str):
                created_at = existing["created_at"]
        _write_atomic_files(
            {
                session_path: yaml.safe_dump(
                    {
                        "id": ref.id,
                        "role": ref.role,
                        "engine": ref.engine,
                        "status": ref.status,
                        "sandboxed": ref.sandboxed,
                        "sandbox": ref.sandbox_summary or "host",
                        "created_at": created_at,
                        "updated_at": utcnow(),
                        "pid": pid,
                        "exit_code": exit_code,
                        "resource_control": resource_control,
                        "resource_limit_event": (
                            None
                            if resource_limit_event is None
                            else resource_limit_event.model_dump(mode="python")
                        ),
                    },
                    sort_keys=False,
                ),
                base / "prompt.txt": prompt,
                base / "transcript.md": transcript,
                base / "report.yaml": yaml.safe_dump(report_payload, sort_keys=False),
            }
        )
        _write_stream_artifact(base, "stdout", stdout, compress=False)
        _write_stream_artifact(base, "stderr", stderr, compress=False)


class _SandboxedAdapter(ExternalCLIAdapter):
    def __init__(self, adapter: ExternalCLIAdapter, launcher: SandboxLauncher, engine_name: str) -> None:
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
    ) -> list[str]:
        return self._adapter.build_command(prompt, cwd, model=model, max_turns=max_turns)

    def detect_capabilities(self):
        return self._adapter.detect_capabilities()

    def finalize_invocation(self, invocation):
        return self._launcher.wrap_invocation(self._engine_name, self.binary, invocation)

    def sandbox_details(self) -> tuple[bool, str]:
        return (self._summary.enabled, self._summary.summary)

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


def stage_prompt(
    task: TaskRecord,
    step: str,
    workspace_context: str = "",
    *,
    process_profile: str = "generic",
) -> str:
    """Build the prompt for a stage subagent."""
    profile = resolve_process_profile(process_profile)
    workspace_overlay = profile.get("workspace_overlay", [])
    stage_overlay = profile.get("stage_overlay", {}).get(step, [])
    stage_instructions = profile.get("stage_instructions", {}).get(step, ["Complete the requested stage."])

    lines = [
        f"Task: {task.id} {task.title}",
        f"Stage: {step}",
        f"Process profile: {profile['label']}",
        f"Task type: {task.task_type or '-'}",
        "",
        "Workspace context:",
        workspace_context.strip() or "No workspace context provided.",
        "",
        "Shared process:",
        f"- Orchestrator model: {profile['orchestrator_model']}",
        f"- Routing model: {profile['routing_model']}",
        f"- Shared stages: {' -> '.join(profile['shared_stages'])}.",
        f"- Role model: {profile['role_model']}",
        f"- Source of truth: {profile['source_of_truth']}",
        f"- Task source of truth: {profile['task_source_of_truth']}",
        f"- TDD expectations: {profile['tdd_expectations']}",
        f"- Verification discipline: {profile['verification_discipline']}",
        f"- Acceptance flow: {profile['acceptance_flow']}",
        f"- Commit and recovery: {profile['commit_recovery']}",
        "",
        "Project overlay:",
        f"- {profile['summary']}",
    ]
    lines.extend(workspace_overlay or ["- No project-specific overlay provided."])
    lines.extend(
        [
            "",
            "Prompt scaffold:",
            *profile.get("prompt_scaffold", []),
            "",
            "Stage instructions:",
            *stage_instructions,
        ]
    )
    lines.extend(stage_overlay)
    lines.extend(
        [
            "",
            "Goal:",
            task.goal or task.title,
            "",
            "Acceptance criteria:",
        ]
    )
    if task.acceptance_criteria:
        lines.extend(f"- {item}" for item in task.acceptance_criteria)
    else:
        lines.append("- No acceptance criteria defined.")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        inferred_acceptance_criteria = infer_acceptance_criteria(task)
        lines.extend(["", "Acceptance gate:"])
        if step == "grooming" and inferred_acceptance_criteria:
            lines.extend(
                [
                    "- Structured acceptance criteria are still missing on the task record, but the current task context is sufficient to infer them.",
                    "- As the PM for grooming, either provide explicit `ACCEPTANCE_CRITERIA:` bullets or let the runner persist the inferred version by returning `VERDICT: PASS`.",
                    "- You may return `VERDICT: PASS` without restating them; the runner will infer and persist the criteria after grooming.",
                    "- If the current task context is not sufficient after all, return `VERDICT: BLOCKED` instead of passing grooming without criteria.",
                    "- To override the inferred version, you may add an `ACCEPTANCE_CRITERIA:` section with concrete `- ` bullets that can be persisted directly.",
                    "- Return `VERDICT: BLOCKED` only if the inferred criteria are incomplete or incorrect and the task still needs more information.",
                    "",
                    "Inferred acceptance criteria available from current task context:",
                ]
            )
            lines.extend(f"- {item}" for item in inferred_acceptance_criteria)
        else:
            lines.extend(
                [
                    f"- {missing_criteria_reason}",
                    "- Use grooming or task intake to define the missing criteria before implementation starts.",
                ]
            )
            if step == "grooming":
                lines.extend(
                    [
                        "- As the PM for grooming, provide an `ACCEPTANCE_CRITERIA:` section with concrete `- ` bullets before passing grooming.",
                        "- If the context is still insufficient, return `VERDICT: BLOCKED` and explain the missing information in `SUMMARY` or `WARNINGS`.",
                    ]
                )

    template = task_template(task)
    if template is not None:
        lines.extend(
            [
                "",
                "Task template:",
                f"- Use the `{task.task_type}` template to keep the task structured.",
            ]
        )
        prompt_guidance = template.get("prompt_guidance", [])
        if isinstance(prompt_guidance, list):
            lines.extend(f"- {item}" for item in prompt_guidance)
        brief_sections = template.get("brief_sections", [])
        if isinstance(brief_sections, list):
            lines.extend(["", "Template sections to fill or verify:"])
            lines.extend(f"- {item}" for item in brief_sections)

    lines.extend(["", "Plan:"])
    if task.plan:
        lines.extend(f"- {item}" for item in task.plan)
    else:
        lines.append("- No plan defined.")

    lines.extend(["", "Constraints:"])
    if task.constraints:
        lines.extend(f"- {item}" for item in task.constraints)
    else:
        lines.append("- Keep changes scoped to the task.")

    lines.extend(
        [
            "",
            "Return exactly this structure:",
            "VERDICT: PASS|FAIL|REJECT|BLOCKED",
            "SUMMARY: one-line summary",
            "FILES_CHANGED:",
            "- path/to/file",
            "TESTS_ADDED: <integer>",
            "TESTS_PASSING: <integer>",
            "WARNINGS:",
            "- optional warning",
        ]
    )
    if step in {"grooming", "accepting"}:
        lines.extend(
            [
                "FOLLOW_UP_TASKS:",
                '[{"title":"optional follow-up title","rationale":"why this separate task is needed","blocking":false}]',
                "- Use a JSON array on the line(s) after `FOLLOW_UP_TASKS:` when you discover separate follow-up work.",
                "- Set `blocking` to `true` only when the extra work blocks the current task from continuing.",
                "- Optional keys per follow-up: `goal`, `acceptance_criteria` (array of strings), `task_type`.",
            ]
        )
    if step == "grooming" and missing_criteria_reason is not None:
        lines.extend(
            [
                "ACCEPTANCE_CRITERIA:",
                "- optional criterion",
            ]
        )
    return "\n".join(lines)


def stage_report_from_subagent(task: TaskRecord, step: str, result: SubagentResult) -> StageReport:
    if (
        result.failure is not None
        and result.failure.kind == "resource_limit"
        and result.failure.resource_limit_event is not None
    ):
        event = result.failure.resource_limit_event
        return StageReport(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            verdict="blocked",
            summary=f"{step} blocked: {event.reason}",
            feedback=result.transcript,
            warnings=[event.reason],
            resource_limit_event=event,
        )
    if result.execution is not None:
        engine = get_engine(result.ref.engine)
        return engine.parse_stage_report(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            execution=result.execution,
            subagent_status=result.ref.status,
        )
    return parse_stage_report_text(
        task_id=task.id,
        step=step,  # type: ignore[arg-type]
        transcript=result.transcript,
        subagent_status=result.ref.status,
    )


def intake_prompt(brain_dump: str) -> str:
    """Build a prompt to analyze a freeform brain dump and suggest a task title and goal."""
    profile = resolve_process_profile("codehive")
    specifics = "\n".join(str(item) for item in profile.get("specifics", []))
    return f"""You are the PM for a local multi-agent coding workspace.
You are handling freeform task intake for a Codehive-style workflow.

Codehive-style specifics:
{specifics}

Analyze the following freeform specification or brain dump and turn it into a rough queued task description.
Produce only a concise title and a short goal statement that preserve the user's intent.
Do not add acceptance criteria, implementation plans, decomposition, or detailed structure.
Keep the scope high-level and reviewable so PM grooming can refine it later.
Treat the original dump as the authoritative source of detail.

Return your suggestion in exactly this format:

TITLE: <concise rough task title>
GOAL: <1-3 sentence high-level goal statement>

--- BRAIN DUMP ---
{brain_dump.strip()}
"""
