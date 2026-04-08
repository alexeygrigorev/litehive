"""Subagent execution management: manager, sandbox adapter, and artifact helpers."""

from dataclasses import dataclass, replace
import inspect
import os
from pathlib import Path
import re
import signal
import time

import yaml

from litehive.config import load_config
from litehive.observability import record_engine_execution, record_engine_observation
from litehive.events import append_event, append_session_log, ensure_session_log
from litehive.engines.base import CLIExecutionResult, ExternalCLIAdapter, parse_stage_report_text
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
from litehive.engines.sandbox import SandboxError, SandboxLauncher
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
_ORIGINAL_EXTERNAL_ADAPTER_RUN = ExternalCLIAdapter.run
_ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE = ExternalCLIAdapter.run_live


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


def _has_callable_override(engine: object, name: str, default: object) -> bool:
    method = getattr(engine, name, None)
    if not callable(method):
        return False
    resolved = _unwrap_bound_callable(method)
    if name == "run" and resolved is _ORIGINAL_EXTERNAL_ADAPTER_RUN:
        return False
    if name == "run_live" and resolved is _ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE:
        return False
    instance_dict = getattr(engine, "__dict__", None)
    if isinstance(instance_dict, dict) and name in instance_dict:
        value = instance_dict[name]
        if callable(value):
            rebound = _unwrap_bound_callable(value)
            if inspect.ismethod(value) and getattr(value, "__self__", None) is engine:
                return False
            for cls in type(engine).__mro__:
                inherited = cls.__dict__.get(name)
                if callable(inherited) and _unwrap_bound_callable(inherited) is rebound:
                    return False
    return resolved is not default


def _callable_resolution_rank(engine: object, name: str) -> int | None:
    target = getattr(engine, name, None)
    if not callable(target):
        return None
    target_impl = _unwrap_bound_callable(target)
    instance_dict = getattr(engine, "__dict__", None)
    if isinstance(instance_dict, dict) and name in instance_dict:
        value = instance_dict[name]
        if callable(value):
            resolved = _unwrap_bound_callable(value)
            for index, cls in enumerate(type(engine).__mro__):
                inherited = cls.__dict__.get(name)
                if callable(inherited) and _unwrap_bound_callable(inherited) is resolved:
                    return index
            return -1
    for index, cls in enumerate(type(engine).__mro__):
        value = cls.__dict__.get(name)
        if callable(value):
            resolved = _unwrap_bound_callable(value)
            for parent_index, parent in enumerate(type(engine).__mro__[index + 1 :], start=index + 1):
                inherited = parent.__dict__.get(name)
                if callable(inherited) and _unwrap_bound_callable(inherited) is resolved:
                    return parent_index
            return index
    return None


def _prefers_non_live_run(engine: object) -> bool:
    if not _has_callable_override(engine, "run", _ORIGINAL_EXTERNAL_ADAPTER_RUN):
        return False
    if not _has_callable_override(engine, "run_live", _ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE):
        return True
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
            if _supports_live_execution(execution_engine):
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
                if _supports_live_on_started(execution_engine):
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
                if _supports_on_started(execution_engine):
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
            is not _ORIGINAL_EXTERNAL_ADAPTER_RUN
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
        inactivity_timeout_seconds: float = 0,
    ) -> CLIExecutionResult:
        if (
            _unwrap_bound_callable(getattr(self._adapter, "run_live", None))
            is not _ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE
        ):
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
