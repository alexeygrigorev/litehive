"""SubagentManager: run external CLI subagents inside a task-scoped folder."""

from dataclasses import replace
import logging
from pathlib import Path
import re

from litehive.config.loading import load_config
from heru import get_engine
from heru.adapters import (
    EngineError,
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
)
from heru.base import CLIExecutionResult, ExternalCLIAdapter
from litehive.agents.sandbox import SandboxError, SandboxLauncher
from litehive.observability.events import append_event
from heru.types import SubagentRef
from litehive.domain.common import cap_feedback
from litehive.domain.reports import StageReport
from litehive.domain.runtime import ResourceLimitEvent
from litehive.domain.task import TaskRecord
from litehive.observability.engine_monitoring import record_engine_execution, record_engine_observation
from litehive.agents.artifacts import (
    prune_superseded_subagent_artifacts,
    write_stream_artifact,
    write_text_if_changed,
)
from heru.engine_detection import (
    effective_engine_callable,
    filter_supported_kwargs,
    supports_live_execution,
    supports_live_on_started,
    supports_on_started,
)
from litehive.domain.agent import EngineFailure, SubagentInactivityTimeout, SubagentResult
from litehive.agents.parsing import stage_report_from_subagent
from litehive.agents.sandbox import SandboxedAdapter
from litehive.agents.session import SessionMixin
from litehive.state.records import save_task
from litehive.tasks.paths import task_dir
from litehive.tasks.runtime import (
    mark_subagent_finished,
    mark_subagent_progress,
    mark_subagent_started,
)
from litehive.tasks.reports import record_stage_report

_REPORTABLE_STAGES = {"grooming", "implementing", "testing", "accepting", "commit_to_git"}
_DEFAULT_STAGE_FOR_ROLE = {
    "planner": "grooming",
    "swe": "implementing",
    "qa": "testing",
    "reviewer": "accepting",
    "merge-resolver": "merge_resolving",
    "recovery": "recovering",
}

logger = logging.getLogger(__name__)


class SubagentManager(SessionMixin):
    """Run external CLI subagents inside a task-scoped folder."""

    def __init__(self, root: Path, *, execution_root: Path | None = None) -> None:
        self.root = root.resolve()
        self.execution_root = (execution_root or root).resolve()
        self.config = load_config(self.root)
        self.sandbox = SandboxLauncher(self.root, self.config)
        self._stream_offsets: dict[str, int] = {}

    @staticmethod
    def _merged_warnings(base: list[str], extra: list[str]) -> list[str]:
        merged = list(base)
        for warning in extra:
            if warning not in merged:
                merged.append(warning)
        return merged

    def _record_live_callback_failure(
        self,
        *,
        ref: SubagentRef,
        phase: str,
        exc: Exception,
        warnings: list[str],
    ) -> None:
        warning = f"runner {phase} bookkeeping failed: {type(exc).__name__}: {exc}"
        if warning not in warnings:
            warnings.append(warning)
        logger.exception(
            "Subagent %s %s callback failed; continuing without crashing the runner",
            ref.id,
            phase,
        )

    @staticmethod
    def _agent_stage_for_task(task: TaskRecord, role: str | None = None) -> str:
        current_stage = task.runtime.current_stage.stage
        if current_stage:
            return current_stage
        pipeline_stage = str(task.pipeline_status) if task.pipeline_status else ""
        if pipeline_stage in _REPORTABLE_STAGES or pipeline_stage in {"merge_resolving", "recovering"}:
            return pipeline_stage
        if role and role in _DEFAULT_STAGE_FOR_ROLE:
            return _DEFAULT_STAGE_FOR_ROLE[role]
        return "implementing"

    @classmethod
    def _report_stage_for_task(cls, task: TaskRecord, role: str | None = None) -> str:
        stage = cls._agent_stage_for_task(task, role)
        if stage in _REPORTABLE_STAGES or stage == "recovering":
            return stage
        if stage == "merge_resolving":
            return "merge_resolving"
        return "implementing"

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
        sandbox_summary = self.sandbox.policy_summary(engine_name, role)
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
        callback_warnings: list[str] = []

        def _safe_on_started(pid: int) -> None:
            try:
                self._record_subagent_pid(task, base, ref, pid)
            except Exception as exc:  # callback failures must not crash the runner
                self._record_live_callback_failure(
                    ref=ref,
                    phase="start",
                    exc=exc,
                    warnings=callback_warnings,
                )

        def _safe_on_update(execution: CLIExecutionResult) -> None:
            try:
                self._write_session_progress(
                    task,
                    base,
                    ref,
                    prompt,
                    execution,
                )
            except Exception as exc:  # progress persistence must not crash the runner
                self._record_live_callback_failure(
                    ref=ref,
                    phase="progress",
                    exc=exc,
                    warnings=callback_warnings,
                )

        try:
            if not engine.is_available():
                raise EngineError(f"Engine '{engine.name}' is unavailable: missing binary '{engine.binary}'")
            if isinstance(engine, ExternalCLIAdapter) and sandbox_summary.enabled:
                execution_engine = SandboxedAdapter(engine, self.sandbox, engine_name, role)
            # Probe the wrapped adapter for capability preference. The sandbox wrapper
            # exposes both run and run_live, so inspecting the wrapper would hide
            # whether the underlying engine actually prefers a custom run override.
            live_execution_probe = engine if execution_engine is not engine else execution_engine
            callback_probe = live_execution_probe
            task_env = {
                "LITEHIVE_TASK_ID": task.id,
                "LITEHIVE_WORKSPACE_ROOT": str(self.root),
                "LITEHIVE_AGENT_ROLE": role,
                "LITEHIVE_STAGE": self._agent_stage_for_task(task, role),
            }
            if supports_live_execution(live_execution_probe):
                run_live_callable = effective_engine_callable(execution_engine, "run_live")
                if not callable(run_live_callable):
                    run_live_callable = execution_engine.run_live
                inactivity_timeout_seconds = self._subagent_inactivity_timeout_seconds(engine_name)
                live_kwargs: dict[str, object] = {
                    "cwd": self.execution_root,
                    "model": model,
                    "emit_unified": True,
                    "extra_env": task_env,
                    "on_update": _safe_on_update,
                }
                if resume_session_id:
                    live_kwargs["resume_session_id"] = resume_session_id
                if supports_live_on_started(callback_probe):
                    live_kwargs["on_started"] = _safe_on_started
                if max_turns is not None:
                    live_kwargs["max_turns"] = max_turns
                if inactivity_timeout_seconds > 0:
                    live_kwargs["inactivity_timeout_seconds"] = inactivity_timeout_seconds
                proc = run_live_callable(
                    prompt,
                    **filter_supported_kwargs(run_live_callable, live_kwargs),
                )
            else:
                run_callable = effective_engine_callable(execution_engine, "run")
                if not callable(run_callable):
                    run_callable = execution_engine.run
                run_kwargs: dict[str, object] = {
                    "cwd": self.execution_root,
                    "model": model,
                    "emit_unified": True,
                    "extra_env": task_env,
                }
                if resume_session_id:
                    run_kwargs["resume_session_id"] = resume_session_id
                if max_turns is not None:
                    run_kwargs["max_turns"] = max_turns
                if supports_on_started(callback_probe):
                    run_kwargs["on_started"] = _safe_on_started
                proc = run_callable(
                    prompt,
                    **filter_supported_kwargs(run_callable, run_kwargs),
                )
            completed_timeout = self._completed_inactivity_timeout(engine_name, proc)
            if completed_timeout is not None:
                raise completed_timeout
            transcript = self._render_execution_transcript(
                ref.engine,
                proc,
                fallback_renderer=execution_engine.render_transcript,
            )
            continuation = self._extract_execution_continuation(ref.engine, proc)
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
            transcript = self._render_execution_transcript(
                ref.engine,
                proc,
                fallback_renderer=execution_engine.render_transcript,
            )
            continuation = self._extract_execution_continuation(ref.engine, proc)
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
                None if failure is None or failure.kind != "execution_interrupted" else failure.reason
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
                None if failure is None or failure.kind != "execution_interrupted" else failure.reason
            ),
            resource_limit_event=None if failure is None else failure.resource_limit_event,
            continuation=continuation,
            extra_warnings=callback_warnings,
        )
        prune_superseded_subagent_artifacts(task_dir(self.root, task), keep_subagent_id=ref.id)
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
            continuation=continuation,
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
        extra_warnings: list[str],
    ) -> None:
        report_stage = self._report_stage_for_task(task, ref.role)
        if resource_limit_event is not None:
            report = StageReport(
                task_id=task.id,
                stage=report_stage,  # type: ignore[arg-type]
                verdict="blocked",
                summary=f"{report_stage} blocked: {resource_limit_event.reason}",
                feedback=cap_feedback(transcript),
                warnings=[resource_limit_event.reason],
                resource_limit_event=resource_limit_event,
            )
        else:
            report = self._parse_execution_report(
                task=task,
                stage=report_stage,
                ref=ref,
                execution=execution,
                transcript=transcript,
            )
        report = report.model_copy(update={"warnings": self._merged_warnings(report.warnings, extra_warnings)})
        record_stage_report(self.root, task, report)
        self._write_session_snapshot(
            task,
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
                "resource_control": self.sandbox.policy_summary(ref.engine, ref.role).as_dict(),
                "interruption_reason": interruption_reason,
                "resource_limit_event": (
                    None
                    if report.resource_limit_event is None
                    else report.resource_limit_event.model_dump(mode="python")
                ),
                "continuation": None if continuation is None else continuation.model_dump(mode="python"),
            },
            exit_code=exit_code,
            pid=None if execution is None else execution.pid,
            interruption_reason=interruption_reason,
            resource_limit_event=resource_limit_event,
            continuation=continuation,
        )
        write_stream_artifact(base, "stdout", "" if execution is None else execution.stdout, compress=True)
        write_stream_artifact(base, "stderr", "" if execution is None else execution.stderr, compress=True)
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
        transcript = self._render_execution_transcript(
            ref.engine,
            execution,
            fallback_renderer=engine.render_transcript,
        )
        continuation = self._extract_execution_continuation(ref.engine, execution)
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
            task,
            base,
            ref,
            exit_code=None,
            pid=execution.pid,
            interruption_reason=None,
            continuation=continuation,
        )
        write_text_if_changed(base / "prompt.txt", prompt)
        write_text_if_changed(base / "transcript.md", transcript)
        write_text_if_changed(base / "stdout.txt", execution.stdout)
        write_text_if_changed(base / "stderr.txt", execution.stderr)
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
        report_stage = self._report_stage_for_task(task, ref.role)
        report_payload = {
            "status": ref.status,
            "summary": "",
            "files_changed": [],
            "tests": {"added": 0, "passing": 0},
            "warnings": [],
            "resource_control": self.sandbox.policy_summary(ref.engine, ref.role).as_dict(),
            "interruption_reason": None,
            "resource_limit_event": None,
        }
        if transcript.strip():
            report = self._parse_execution_report(
                task=task,
                stage=report_stage,
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
                "resource_control": self.sandbox.policy_summary(ref.engine, ref.role).as_dict(),
                "resource_limit_event": (
                    None
                    if report.resource_limit_event is None
                    else report.resource_limit_event.model_dump(mode="python")
                ),
                "continuation": None if continuation is None else continuation.model_dump(mode="python"),
            }
        self._write_session_snapshot(
            task,
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
        self._check_stdout_inactivity(base, ref.engine, execution)

    def _parse_execution_report(
        self,
        *,
        task: TaskRecord,
        stage: str,
        ref: SubagentRef,
        execution: CLIExecutionResult | None,
        transcript: str,
    ) -> StageReport:
        return stage_report_from_subagent(
            task,
            stage,
            SubagentResult(
                ref=ref,
                execution=execution,
                transcript=transcript,
                exit_code=0 if execution is None else execution.exit_code,
            ),
            root=self.root,
        )
