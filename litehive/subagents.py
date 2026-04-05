"""Subagent execution and folder persistence."""


from dataclasses import dataclass, replace
import inspect
import os
from pathlib import Path
import re
import signal
import time

import yaml

from litehive.config import LitehiveConfig, load_config, resolve_process_profile
from litehive.engine_monitoring import record_engine_execution, record_engine_observation
from litehive.events import append_event, append_session_log, ensure_session_log
from litehive.external_cli import CLIExecutionResult, ExternalCLIAdapter, parse_stage_report_text
from litehive.engines import (
    EngineError,
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
    extract_engine_continuation,
    extract_engine_timeline,
    get_engine,
)
from litehive.models import (
    ResourceLimitEvent,
    StageReport,
    SubagentRef,
    TaskRecord,
    cap_feedback,
    utcnow,
)
from litehive.sandbox import SandboxError, SandboxLauncher
from litehive.tasks import (
    _atomic_write_gzip_text,
    _write_atomic_files,
    infer_acceptance_criteria,
    mark_subagent_finished,
    mark_subagent_progress,
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


class SubagentInactivityTimeout(RuntimeError):
    """Raised when a live subagent stops producing stdout for too long."""

    def __init__(
        self, execution: CLIExecutionResult, *, idle_seconds: float, limit_seconds: float
    ) -> None:
        self.execution = execution
        self.idle_seconds = idle_seconds
        self.limit_seconds = limit_seconds
        super().__init__(
            "litehive killed stale subagent after "
            f"{limit_seconds:g}s without new stdout (idle {idle_seconds:.1f}s)"
        )


_COMPRESS_STREAM_ARTIFACT_MIN_BYTES = 4096
_COMPRESS_TEXT_ARTIFACT_MIN_BYTES = 4096


def _write_stream_artifact(base: Path, name: str, content: str, *, compress: bool) -> None:
    plain_path = base / f"{name}.txt"
    compressed_path = base / f"{name}.txt.gz"
    if compress and not content:
        if plain_path.exists():
            plain_path.unlink()
        if compressed_path.exists():
            compressed_path.unlink()
        return
    should_compress = (
        compress and len(content.encode("utf-8")) >= _COMPRESS_STREAM_ARTIFACT_MIN_BYTES
    )
    if should_compress:
        if plain_path.exists():
            plain_path.unlink()
        _atomic_write_gzip_text(compressed_path, content)
        return
    if compressed_path.exists():
        compressed_path.unlink()
    _write_text_if_changed(plain_path, content)


def _write_text_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    _write_atomic_files({path: content})
    return True


def _write_text_artifact(
    base: Path,
    name: str,
    suffix: str,
    content: str,
    *,
    compress: bool,
) -> Path:
    plain_path = base / f"{name}{suffix}"
    compressed_path = base / f"{name}{suffix}.gz"
    should_compress = compress and len(content.encode("utf-8")) >= _COMPRESS_TEXT_ARTIFACT_MIN_BYTES
    if should_compress:
        if plain_path.exists():
            plain_path.unlink()
        _atomic_write_gzip_text(compressed_path, content)
        return compressed_path
    if compressed_path.exists():
        compressed_path.unlink()
    _write_atomic_files({plain_path: content})
    return plain_path


def _prune_superseded_subagent_artifacts(task_root: Path, *, keep_subagent_id: str) -> None:
    subagents_root = task_root / "subagents"
    if not subagents_root.exists():
        return
    raw_names = (
        "prompt.txt",
        "transcript.md",
        "transcript.md.gz",
        "stdout.log",
        "stdout.txt",
        "stdout.txt.gz",
        "stderr.log",
        "stderr.txt",
        "stderr.txt.gz",
        "timeline.yaml",
        "timeline.yaml.gz",
    )
    prefix = f"{keep_subagent_id}-"
    for child in subagents_root.iterdir():
        if not child.is_dir() or child.name.startswith(prefix):
            continue
        for name in raw_names:
            (child / name).unlink(missing_ok=True)


def _supports_live_execution(engine: object) -> bool:
    run_live = getattr(engine, "run_live", None)
    if not callable(run_live):
        return False
    return not _prefers_non_live_run(engine)


def _unwrap_bound_callable(method: object) -> object:
    return getattr(method, "__func__", method)


def _callable_resolution_rank(engine: object, name: str) -> int | None:
    instance_dict = getattr(engine, "__dict__", None)
    if isinstance(instance_dict, dict) and name in instance_dict:
        value = instance_dict[name]
        if callable(value):
            return -1
    for index, cls in enumerate(type(engine).__mro__):
        value = cls.__dict__.get(name)
        if callable(value):
            return index
    return None


def _prefers_non_live_run(engine: object) -> bool:
    run_impl = _unwrap_bound_callable(getattr(engine, "run", None))
    run_live_impl = _unwrap_bound_callable(getattr(engine, "run_live", None))
    if run_impl is ExternalCLIAdapter.run:
        return False
    run_rank = _callable_resolution_rank(engine, "run")
    run_live_rank = _callable_resolution_rank(engine, "run_live")
    if run_rank is None:
        return False
    if run_live_rank is None:
        return True
    return run_rank < run_live_rank


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

    def __init__(self, root: Path, *, execution_root: Path | None = None) -> None:
        self.root = root.resolve()
        self.execution_root = (execution_root or root).resolve()
        self.config = load_config(self.root)
        self.sandbox = SandboxLauncher(self.root, self.config)
        self._stream_offsets: dict[str, int] = {}

    def run(
        self,
        task: TaskRecord,
        *,
        role: str,
        engine_name: str,
        prompt: str,
        model: str | None = None,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> SubagentResult:
        subagent_id = self._next_subagent_id(task)
        folder_name = f"{subagent_id}-{role}"
        base = task_dir(self.root, task) / "subagents" / folder_name
        base.mkdir(parents=True, exist_ok=False)

        engine = get_engine(engine_name)
        execution_engine = engine
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
        self._write_session_start(task, base, ref, prompt)
        failure: EngineFailure | None = None
        try:
            if not engine.is_available():
                raise EngineError(
                    f"Engine '{engine.name}' is unavailable: missing binary '{engine.binary}'"
                )
            if isinstance(engine, ExternalCLIAdapter) and sandbox_summary.enabled:
                execution_engine = _SandboxedAdapter(engine, self.sandbox, engine_name)
            if _supports_live_execution(engine):
                live_kwargs: dict[str, object] = {
                    "cwd": self.execution_root,
                    "model": model,
                    "on_update": lambda execution: self._write_session_progress(
                        task,
                        base,
                        ref,
                        prompt,
                        execution,
                    ),
                }
                if resume_session_id:
                    live_kwargs["resume_session_id"] = resume_session_id
                if _supports_live_on_started(engine):
                    live_kwargs["on_started"] = lambda pid: self._record_subagent_pid(
                        task, base, ref, pid
                    )
                if max_turns is not None:
                    live_kwargs["max_turns"] = max_turns
                if self.config.subagent_inactivity_timeout_seconds > 0:
                    live_kwargs["inactivity_timeout_seconds"] = (
                        self.config.subagent_inactivity_timeout_seconds
                    )
                proc = execution_engine.run_live(prompt, **live_kwargs)
            else:
                run_kwargs: dict[str, object] = {
                    "cwd": self.execution_root,
                    "model": model,
                }
                if resume_session_id:
                    run_kwargs["resume_session_id"] = resume_session_id
                if max_turns is not None:
                    run_kwargs["max_turns"] = max_turns
                if _supports_on_started(engine):
                    run_kwargs["on_started"] = lambda pid: self._record_subagent_pid(
                        task, base, ref, pid
                    )
                proc = execution_engine.run(prompt, **run_kwargs)
            transcript = execution_engine.render_transcript(proc)
            continuation = extract_engine_continuation(ref.engine, proc)
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
                    interruption_reason = classify_execution_interruption(
                        transcript,
                        exit_code=proc.exit_code,
                    )
                    if interruption_reason is not None:
                        ref.status = "interrupted"
                        failure = EngineFailure(
                            kind="execution_interrupted",
                            reason=interruption_reason,
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
        except SubagentInactivityTimeout as exc:
            timeout_note = str(exc)
            stderr = exc.execution.stderr
            if timeout_note not in stderr:
                stderr = f"{stderr.rstrip()}\n{timeout_note}".strip()
            proc = replace(exc.execution, exit_code=124, stderr=stderr)
            transcript = execution_engine.render_transcript(proc)
            continuation = extract_engine_continuation(ref.engine, proc)
            ref.status = "failed"
            failure = EngineFailure(
                kind="retryable_execution_error",
                reason="transient timeout",
                classification="timeout",
            )
        except (EngineError, SandboxError) as exc:
            transcript = str(exc)
            proc = None
            continuation = None
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
            interruption_reason=(
                None
                if failure is None or failure.kind != "execution_interrupted"
                else failure.reason
            ),
            resource_limit_event=None if failure is None else failure.resource_limit_event,
            continuation=continuation,
        )
        self._write_session_finish(
            task,
            base,
            ref,
            prompt,
            transcript,
            0 if proc is None else proc.exit_code,
            proc,
            interruption_reason=(
                None
                if failure is None or failure.kind != "execution_interrupted"
                else failure.reason
            ),
            resource_limit_event=None if failure is None else failure.resource_limit_event,
            continuation=continuation,
        )
        _prune_superseded_subagent_artifacts(task_dir(self.root, task), keep_subagent_id=ref.id)
        if proc is not None:
            record_engine_execution(
                self.root,
                task_id=task.id,
                engine_name=engine_name,
                adapter=execution_engine,
                execution=proc,
                failure_kind=None if failure is None else failure.kind,
                failure_reason=None if failure is None else failure.reason,
            )
        return SubagentResult(
            ref=ref,
            execution=proc,
            transcript=transcript,
            exit_code=0 if proc is None else proc.exit_code,
            failure=failure,
        )

    def _append_stream_delta(
        self, base: Path, ref: SubagentRef, stream: str, full_content: str
    ) -> None:
        """Append only the new portion of a stream to the append-only log."""
        key = f"{ref.id}:{stream}"
        prev = self._stream_offsets.get(key, 0)
        if len(full_content) > prev:
            append_session_log(base, stream, full_content[prev:])
            self._stream_offsets[key] = len(full_content)

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
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
        prompt: str,
    ) -> None:
        ensure_session_log(base, "stdout")
        ensure_session_log(base, "stderr")
        append_event(
            self.root,
            task,
            "subagent_started",
            data={
                "subagent_id": ref.id,
                "role": ref.role,
                "engine": ref.engine,
                "sandboxed": ref.sandboxed,
            },
        )
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
            interruption_reason=None,
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
        interruption_reason: str | None,
        resource_limit_event: ResourceLimitEvent | None,
        continuation,
    ) -> None:
        report_step = (
            task.pipeline_status
            if task.pipeline_status
            in {"grooming", "implementing", "testing", "accepting", "commit_to_git"}
            else "implementing"
        )
        if resource_limit_event is not None:
            report = StageReport(
                task_id=task.id,
                step=report_step,  # type: ignore[arg-type]
                verdict="blocked",
                summary=f"{report_step} blocked: {resource_limit_event.reason}",
                feedback=cap_feedback(transcript),
                warnings=[resource_limit_event.reason],
                resource_limit_event=resource_limit_event,
            )
        else:
            report = self._parse_execution_report(
                task=task,
                step=report_step,
                ref=ref,
                execution=execution,
                transcript=transcript,
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
                "interruption_reason": interruption_reason,
                "resource_limit_event": (
                    None
                    if report.resource_limit_event is None
                    else report.resource_limit_event.model_dump(mode="python")
                ),
                "continuation": None
                if continuation is None
                else continuation.model_dump(mode="python"),
            },
            exit_code=exit_code,
            pid=None if execution is None else execution.pid,
            interruption_reason=interruption_reason,
            resource_limit_event=resource_limit_event,
            continuation=continuation,
        )
        _write_stream_artifact(
            base, "stdout", "" if execution is None else execution.stdout, compress=True
        )
        _write_stream_artifact(
            base, "stderr", "" if execution is None else execution.stderr, compress=True
        )
        if execution is not None:
            self._append_stream_delta(base, ref, "stdout", execution.stdout)
            self._append_stream_delta(base, ref, "stderr", execution.stderr)
        append_event(
            self.root,
            task,
            "subagent_finished",
            data={
                "subagent_id": ref.id,
                "role": ref.role,
                "engine": ref.engine,
                "status": ref.status,
                "exit_code": exit_code,
                "interruption_reason": interruption_reason,
            },
        )
        self._write_timeline(base, ref, task, "" if execution is None else execution.stdout)

    def _write_session_progress(
        self,
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
        prompt: str,
        execution: CLIExecutionResult,
    ) -> None:
        engine = get_engine(ref.engine)
        transcript = engine.render_transcript(execution)
        continuation = extract_engine_continuation(ref.engine, execution)
        if isinstance(engine, ExternalCLIAdapter):
            record_engine_observation(
                self.root,
                task_id=task.id,
                engine_name=ref.engine,
                adapter=engine,
                execution=execution,
            )
        self._record_subagent_pid(task, base, ref, execution.pid)
        mark_subagent_progress(
            self.root,
            task,
            pid=execution.pid,
            transcript=transcript,
            continuation=continuation,
        )
        self._write_session_metadata(
            base,
            ref,
            exit_code=None,
            pid=execution.pid,
            interruption_reason=None,
            continuation=continuation,
        )
        _write_text_if_changed(base / "prompt.txt", prompt)
        _write_text_if_changed(base / "transcript.md", transcript)
        _write_text_if_changed(base / "stdout.txt", execution.stdout)
        _write_text_if_changed(base / "stderr.txt", execution.stderr)
        self._append_stream_delta(base, ref, "stdout", execution.stdout)
        self._append_stream_delta(base, ref, "stderr", execution.stderr)
        append_event(
            self.root,
            task,
            "subagent_progress",
            data={
                "subagent_id": ref.id,
                "pid": execution.pid,
            },
        )
        report_step = (
            task.pipeline_status
            if task.pipeline_status
            in {"grooming", "implementing", "testing", "accepting", "commit_to_git"}
            else "implementing"
        )
        report_payload = {
            "status": ref.status,
            "summary": "",
            "files_changed": [],
            "tests": {"added": 0, "passing": 0},
            "warnings": [],
            "resource_control": self.sandbox.policy_summary(ref.engine).as_dict(),
            "interruption_reason": None,
            "resource_limit_event": None,
        }
        if transcript.strip():
            report = self._parse_execution_report(
                task=task,
                step=report_step,
                ref=ref,
                execution=execution,
                transcript=transcript,
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
                "continuation": None
                if continuation is None
                else continuation.model_dump(mode="python"),
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
            interruption_reason=None,
            resource_limit_event=None,
            continuation=continuation,
        )
        self._write_timeline(base, ref, task, execution.stdout)
        self._check_stdout_inactivity(base, execution)

    def _parse_execution_report(
        self,
        *,
        task: TaskRecord,
        step: str,
        ref: SubagentRef,
        execution: CLIExecutionResult | None,
        transcript: str,
    ) -> StageReport:
        if execution is not None:
            engine = get_engine(ref.engine)
            if hasattr(engine, "parse_stage_report"):
                return engine.parse_stage_report(
                    task_id=task.id,
                    step=step,  # type: ignore[arg-type]
                    execution=execution,
                    subagent_status=ref.status,
                )
        return parse_stage_report_text(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            transcript=transcript,
            subagent_status=ref.status,
        )

    def _write_session_metadata(
        self,
        base: Path,
        ref: SubagentRef,
        *,
        exit_code: int | None,
        pid: int | None,
        interruption_reason: str | None = None,
        resource_limit_event: ResourceLimitEvent | None = None,
        continuation=None,
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
                        "interruption_reason": interruption_reason,
                        "resource_control": resource_control,
                        "resource_limit_event": (
                            None
                            if resource_limit_event is None
                            else resource_limit_event.model_dump(mode="python")
                        ),
                        "continuation": None
                        if continuation is None
                        else continuation.model_dump(mode="python"),
                    },
                    sort_keys=False,
                )
            }
        )

    def _record_subagent_pid(
        self, task: TaskRecord, base: Path, ref: SubagentRef, pid: int | None
    ) -> None:
        if pid is None:
            return
        mark_subagent_pid(self.root, task, pid)
        self._write_session_metadata(
            base,
            ref,
            exit_code=None,
            pid=pid,
            interruption_reason=None,
            resource_limit_event=None,
            continuation=None,
        )
        append_event(
            self.root,
            task,
            "subagent_pid",
            data={"subagent_id": ref.id, "pid": pid},
        )

    def _check_stdout_inactivity(self, base: Path, execution: CLIExecutionResult) -> None:
        if execution.pid is None:
            return
        stdout_path = base / "stdout.txt"
        if not stdout_path.exists():
            return
        idle_seconds = max(0.0, time.time() - stdout_path.stat().st_mtime)
        if idle_seconds < self.config.subagent_inactivity_timeout_seconds:
            return
        self._terminate_stale_pid(execution.pid)
        raise SubagentInactivityTimeout(
            execution,
            idle_seconds=idle_seconds,
            limit_seconds=self.config.subagent_inactivity_timeout_seconds,
        )

    def _terminate_stale_pid(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def _write_timeline(
        self,
        base: Path,
        ref: SubagentRef,
        task: TaskRecord,
        stdout: str,
    ) -> None:
        timeline = extract_engine_timeline(
            ref.engine,
            stdout,
            task_id=task.id,
            subagent_id=ref.id,
        )
        if timeline is None:
            return
        _write_text_artifact(
            base,
            "timeline",
            ".yaml",
            yaml.safe_dump(timeline.model_dump(mode="python"), sort_keys=False),
            compress=ref.status != "running",
        )

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
        interruption_reason: str | None,
        resource_limit_event: ResourceLimitEvent | None,
        continuation=None,
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
                        "interruption_reason": interruption_reason,
                        "resource_control": resource_control,
                        "resource_limit_event": (
                            None
                            if resource_limit_event is None
                            else resource_limit_event.model_dump(mode="python")
                        ),
                        "continuation": None
                        if continuation is None
                        else continuation.model_dump(mode="python"),
                    },
                    sort_keys=False,
                ),
                base / "report.yaml": yaml.safe_dump(report_payload, sort_keys=False),
            }
        )
        _write_text_artifact(base, "prompt", ".txt", prompt, compress=False)
        _write_text_artifact(
            base,
            "transcript",
            ".md",
            transcript,
            compress=ref.status != "running",
        )
        _write_stream_artifact(base, "stdout", stdout, compress=False)
        _write_stream_artifact(base, "stderr", stderr, compress=False)


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
        if (
            _unwrap_bound_callable(getattr(self._adapter, "run", None))
            is not ExternalCLIAdapter.run
        ):
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
    ) -> CLIExecutionResult:
        if (
            _unwrap_bound_callable(getattr(self._adapter, "run_live", None))
            is not ExternalCLIAdapter.run_live
        ):
            return self._adapter.run_live(
                prompt,
                cwd,
                model=model,
                max_turns=max_turns,
                resume_session_id=resume_session_id,
                on_started=on_started,
                on_update=on_update,
            )
        return super().run_live(
            prompt,
            cwd,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
            on_started=on_started,
            on_update=on_update,
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


def stage_prompt(
    task: TaskRecord,
    step: str,
    workspace_context: str = "",
    *,
    process_profile: str = "generic",
    role_name: str | None = None,
    config: LitehiveConfig | None = None,
    root: Path | None = None,
) -> str:
    """Build the prompt for a stage subagent."""
    profile = resolve_process_profile(process_profile)
    workspace_overlay = profile.get("workspace_overlay", [])
    stage_overlay = profile.get("stage_overlay", {}).get(step, [])
    stage_instructions = profile.get("stage_instructions", {}).get(
        step, ["Complete the requested stage."]
    )
    lifecycle_verification_overlay: list[str] = []
    stage_owner = role_name or _stage_owner_for_step(step)
    stage_role = _stage_role_prompt(step, stage_owner)
    startup_guidance = _agent_startup_guidance(config, stage_owner, root=root)

    lines = [
        f"Task: {task.id} {task.title}",
        f"Stage: {step}",
        f"Stage owner: {stage_owner}",
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
            "Role focus:",
            *stage_role,
        ]
    )
    if startup_guidance:
        lines.extend(
            [
                "",
                "Project startup guidance:",
                *startup_guidance,
            ]
        )
    lines.extend(
        [
            "",
            "Stage instructions:",
            *stage_instructions,
        ]
    )
    lines.extend(stage_overlay)
    lines.extend(lifecycle_verification_overlay)
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
                    "- As the planner for grooming, either provide explicit `ACCEPTANCE_CRITERIA:` bullets or let the runner persist the inferred version by returning `VERDICT: PASS`.",
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
                        "- As the planner for grooming, provide an `ACCEPTANCE_CRITERIA:` section with concrete `- ` bullets before passing grooming.",
                        "- If the context is still insufficient, return `VERDICT: BLOCKED` and explain the missing information in `SUMMARY` or `WARNINGS`.",
                    ]
                )

    if step == "grooming":
        lines.extend(
            [
                "",
                "Acceptance criteria best practices:",
                "- Write each criterion as an observable outcome the user or reviewer can verify, not an implementation step.",
                "- A good criterion answers: what should happen or be true when this task is done?",
                '- Bad: "Add a validation function" — describes code to write, not behavior to verify.',
                '- Good: "Submitting invalid input shows a clear error message" — describes observable behavior.',
                '- Bad: "Refactor the module" — vague, no verifiable outcome.',
                '- Good: "The module exports the same public API and all existing tests still pass" — specific and testable.',
                '- Bad: "Run `npm test`" — just a command, not an expectation.',
                '- Good: "`npm test` passes with zero failures" — ties the command to a verifiable result.',
                "- Each criterion should be independently checkable without reading the implementation.",
                "- Avoid listing commands alone; always pair them with the expected outcome.",
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

    handoff = task.runtime.continuation_handoff
    if handoff is not None and handoff.step == step:
        lines.extend(["", "Continuation handoff:"])
        lines.append(f"- Kind: {handoff.kind}")
        lines.append(f"- Reason: {handoff.reason}")
        if handoff.from_engine or handoff.to_engine:
            lines.append(
                f"- Engine path: {handoff.from_engine or '-'} -> {handoff.to_engine or handoff.from_engine or '-'}"
            )
        if handoff.from_model or handoff.to_model:
            lines.append(
                f"- Model path: {handoff.from_model or '-'} -> {handoff.to_model or handoff.from_model or '-'}"
            )
        if handoff.attempt is not None:
            lines.append(f"- Prior attempt: {handoff.attempt}")
        if handoff.subagent_id or handoff.subagent_path:
            lines.append(
                f"- Prior subagent: {handoff.subagent_id or '-'} at `{handoff.subagent_path or '-'}`"
            )
        if handoff.summary:
            lines.append(f"- Prior summary: {handoff.summary}")
        if handoff.transcript_snippet:
            lines.append(f"- Prior snippet: {handoff.transcript_snippet}")
        if handoff.continuation is not None:
            if handoff.continuation.session_id:
                lines.append(f"- Engine session id: {handoff.continuation.session_id}")
            if handoff.continuation.thread_id:
                lines.append(f"- Engine thread id: {handoff.continuation.thread_id}")
        artifact_parts = [
            path
            for path in (
                handoff.session_path,
                handoff.report_path,
                handoff.transcript_path,
            )
            if path
        ]
        if artifact_parts:
            lines.append(
                f"- Handoff artifacts: {', '.join(f'`{path}`' for path in artifact_parts)}"
            )
        if handoff.warnings:
            lines.extend(["- Prior warnings:"] + [f"  - {warning}" for warning in handoff.warnings])
        lines.extend(
            [
                "- Continue from the prior stage context instead of restarting discovery from scratch.",
                "- Reuse the recorded artifacts and continuation identifiers when they help you preserve progress safely.",
            ]
        )

    lines.extend(["", "Plan:"])
    if task.plan:
        lines.extend(f"- {item}" for item in task.plan)
    else:
        lines.append("- No plan defined.")

    lines.extend(["", "PM sizing:"])
    lines.append(f"- Current PM complexity: {task.pm_complexity or '-'}")
    lines.append(f"- Current planned effort: {task.planned_effort or '-'}")
    if step == "grooming":
        lines.extend(
            [
                "- During grooming, set PM sizing when you have enough context.",
                "- Use `PM_COMPLEXITY: simple|moderate|complex`.",
                "- Use `PLANNED_EFFORT: xs|s|m|l|xl`.",
            ]
        )

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
            "",
            "Preferred: emit a schema-validated JSON block instead of the text above.",
            "Place the JSON on the line(s) after `STAGE_RESULT:`.",
            "STAGE_RESULT:",
            '{"verdict":"pass","summary":"one-line summary","files_changed":["path/to/file"],'
            '"tests":{"added":0,"passing":0},"warnings":[],'
            '"follow_up_tasks":[],"acceptance_criteria":[]}',
            "The text format above is still accepted as a fallback.",
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

    # Include the task discussion thread so agents see the full history
    from litehive.tasks import render_task_thread

    thread_text = render_task_thread(root, task) if root is not None else ""
    if thread_text:
        lines.extend(["", thread_text])

    # Instruct agents to submit their verdict via CLI
    lines.extend(
        [
            "",
            "IMPORTANT: When you are done, you MUST submit your verdict by running:",
            f'  litehive report --verdict <pass|fail|reject|blocked> --role {stage_owner} --step {step} --message "<your report>"',
            "",
            "Your --message is the PRIMARY way the next agent understands what happened.",
            "Do NOT rely on your raw transcript being read — write the report as if it is the only thing the next agent will see.",
            "",
            "Report requirements:",
            "- On PASS/ACCEPT: explain what you verified, what tests you ran, what evidence confirms the acceptance criteria are met.",
            "- On REJECT: you MUST include ALL of the following:",
            "  1. EXPECTED behavior: what should happen according to the acceptance criteria",
            "  2. OBSERVED behavior: what actually happens (exact error messages, test output, wrong values)",
            "  3. Steps to reproduce: the exact command or test that demonstrates the gap",
            "  4. Which acceptance criteria are not met and which ones are already satisfied",
            "- On FAIL: explain what went wrong and whether it is fixable or needs a different approach.",
            "- On BLOCKED: explain what dependency or resource is missing.",
            "",
            "A vague rejection like 'tests fail' or 'missing evidence' is useless and causes infinite loops.",
            "A good rejection looks like: 'Expected: `litehive engine gemini` switches the default engine and prints confirmation. "
            "Observed: command exits 0 but config.yaml still shows the old engine. Reproduce: run `litehive engine gemini` then `cat .litehive/config.yaml`. "
            "Criteria 1-3 are met, criterion 4 (persistence) is not.'",
            "",
            "The text-based VERDICT/SUMMARY format is accepted as fallback but litehive report is strongly preferred.",
        ]
    )

    return "\n".join(lines)


def _stage_owner_for_step(step: str) -> str:
    return {
        "grooming": "planner",
        "implementing": "swe",
        "testing": "qa",
        "accepting": "reviewer",
        "commit_to_git": "runner",
    }.get(step, "swe")


def _stage_role_prompt(step: str, owner: str | None = None) -> list[str]:
    owner = owner or _stage_owner_for_step(step)
    if owner == "recovery":
        return [
            "- You are the recovery agent responsible for diagnosing why this task stopped making progress and restoring a runnable path.",
            "- Inspect the latest failure context, reports, continuation handoff, and existing artifacts before changing code or task state.",
            "- Make the smallest effective fix needed so the task can resume the current stage and finish cleanly.",
            "- Preserve useful progress, avoid restarting discovery from scratch, and keep the task moving toward completion.",
            "- If the failure traceback shows a Litehive-owned bug rather than a project bug, switch into the repo at `litehive_source_path` and repair Litehive there instead of changing the target project.",
            "- For Litehive self-heal work, keep the external-project task as the source of audit history, run `uv run pytest` in the Litehive repo before reporting success, and leave an explicit blocker if `litehive_source_path` is missing or unusable.",
        ]
    if step == "grooming":
        return [
            "- You are the planner, a PM-style role representing the user's and product's point of view.",
            "- Frame the real user problem, clarify scope, sharpen acceptance criteria, decompose the work, identify follow-up tasks, and estimate PM sizing.",
            "- Treat the Litehive CLI as the source of truth for task shaping: use the task record fields directly, and when documenting operator guidance prefer concrete `litehive add`, `litehive update`, and `litehive intake` flows over vague prose.",
            "- Do not pass grooming with a blank task record; make sure the task has a clear goal and explicit acceptance criteria, or return a blocked outcome that names what is missing.",
            "- Do not implement code in this stage.",
        ]
    if step == "accepting":
        return [
            "- You are the reviewer, a PM-style role representing the user's and product's point of view.",
            "- Validate the strict end-user outcome, look for regressions or missing evidence, and make a final done versus not-done judgment.",
            "- Reject work that is incomplete, weakly verified, or misaligned with the promised outcome.",
        ]
    if step == "implementing":
        return [
            "- You are the SWE responsible for completing the implementation within scope.",
            "- Start from the task record, latest report, and latest rejection or recovery artifact before broad repository exploration.",
            "- Treat the task goal, acceptance criteria, and plan as the execution contract; if they are missing or contradictory, route the issue back through grooming or recovery instead of guessing.",
        ]
    if step == "testing":
        return ["- You are the QA verifier responsible for focused independent validation."]
    return ["- Follow the stage instructions and keep the report concise and explicit."]


def _load_agent_md(root: Path | None, role: str) -> list[str] | None:
    """Read .litehive/agents/{role}.md and return content as '- ' prefixed lines, or None."""
    if root is None:
        return None
    md_path = root / ".litehive" / "agents" / f"{role}.md"
    if not md_path.is_file():
        return None
    text = md_path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    return [f"- {line}" for line in text.splitlines()]


def _agent_startup_guidance(
    config: LitehiveConfig | None,
    stage_owner: str,
    *,
    root: Path | None = None,
) -> list[str]:
    lines: list[str] = []
    guidance = config.agent_startup_guidance if config is not None else {}
    for key in ("all", stage_owner):
        md_lines = _load_agent_md(root, key)
        if md_lines is not None:
            lines.extend(md_lines)
        else:
            for item in guidance.get(key, []):
                lines.append(f"- {item}")
    return lines


def stage_report_from_subagent(
    task: TaskRecord,
    step: str,
    result: SubagentResult,
    *,
    root: Path | None = None,
) -> StageReport:
    # Check if agent submitted a verdict via `litehive report`
    if root is not None:
        from litehive.tasks import load_task_thread

        thread = load_task_thread(root, task)
        step_comments = [c for c in thread if c.step == step and c.verdict != "comment"]
        if step_comments:
            latest = step_comments[-1]
            return StageReport(
                task_id=task.id,
                step=step,  # type: ignore[arg-type]
                verdict=latest.verdict,  # type: ignore[arg-type]
                summary=latest.message.splitlines()[0]
                if latest.message
                else f"{step} {latest.verdict}",
                feedback=latest.message,
                files_changed=latest.files_changed,
            )

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
    return f"""You are the planner for a local multi-agent coding workspace.
You are handling freeform task intake for a Codehive-style workflow.

Codehive-style specifics:
{specifics}

Analyze the following freeform specification or brain dump and turn it into a rough queued task description.
Produce only a concise title and a short goal statement that preserve the user's intent.
Do not add acceptance criteria, implementation plans, decomposition, or detailed structure.
Keep the scope high-level and reviewable so planner grooming can refine it later.
Treat the original dump as the authoritative source of detail.

Return your suggestion in exactly this format:

TITLE: <concise rough task title>
GOAL: <1-3 sentence high-level goal statement>

--- BRAIN DUMP ---
{brain_dump.strip()}
"""
