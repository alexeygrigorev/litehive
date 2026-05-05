"""SubagentManager: run external CLI subagents inside a task-scoped folder."""

from dataclasses import replace
import logging
from pathlib import Path
import re
import sys
import time

from litehive.config.loading import load_config
from heru import get_engine, resume_safe_model_override
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
from litehive.domain.reports import REPORT_VERDICT_KINDS, StageReport
from litehive.domain.task import TaskRecord
from litehive.observability.engine_monitoring import record_engine_execution, record_engine_observation
from litehive.agents.artifacts import (
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
from litehive.domain.common import PipelineState, TaskStage
from litehive.tasks.activity import latest_task_activity_entry
from litehive.tasks.activity_rendering import normalized_files_changed
from litehive.tasks.report_storage import record_stage_report

_REPORTABLE_STAGES: frozenset[str] = frozenset(stage.value for stage in TaskStage)
_DEFAULT_STAGE_FOR_ROLE: dict[str, str] = {
    "planner": TaskStage.GROOMING.value,
    "swe": TaskStage.IMPLEMENTING.value,
    "qa": TaskStage.TESTING.value,
    "reviewer": TaskStage.ACCEPTING.value,
    "merge-resolver": PipelineState.MERGE_RESOLVING.value,
    "recovery": PipelineState.RECOVERING.value,
}

logger = logging.getLogger(__name__)


def _latest_report_files_changed(
    root: Path,
    task: TaskRecord,
    pipeline_state: str,
    source_subagent_id: str | None = None,
) -> list[str]:
    """Read the most recent activity entry's normalized file list so finish/progress snapshots show *what this subagent actually touched*, not whatever the subagent self-reported."""
    latest = latest_task_activity_entry(
        root,
        task,
        stage=pipeline_state,
        source_subagent_id=source_subagent_id,
        verdicts=REPORT_VERDICT_KINDS,
    )
    if latest is None:
        return []
    return normalized_files_changed(latest.files_changed)


def _check_engine_availability_with_retry(engine, max_retries: int = 2, delay: float = 0.5) -> bool:
    """Check engine availability with retry logic for transient failures.

    Some engine availability checks can fail transiently due to filesystem
    or PATH issues during subprocess execution. This adds retry logic to
    avoid task failures due to temporary environmental problems.

    Args:
        engine: The engine instance to check
        max_retries: Maximum number of retry attempts
        delay: Delay between retries in seconds

    Returns:
        True if engine is available, False otherwise
    """
    for attempt in range(max_retries + 1):
        try:
            if engine.is_available():
                if attempt > 0:
                    logger.info(f"Engine {engine.name} availability check succeeded on retry {attempt}")
                return True
        except Exception as exc:
            if attempt < max_retries:
                logger.warning(
                    f"Engine {engine.name} availability check failed (attempt {attempt + 1}/{max_retries + 1}): {exc}. Retrying..."
                )
                time.sleep(delay)
            else:
                logger.warning(
                    f"Engine {engine.name} availability check failed after {max_retries + 1} attempts: {exc}"
                )
                return False

        if attempt < max_retries:
            logger.warning(
                f"Engine {engine.name} reported unavailable (attempt {attempt + 1}/{max_retries + 1}). Retrying..."
            )
            time.sleep(delay)

    return False


class SubagentStartupError(RuntimeError):
    """Unexpected failure before the engine subprocess started."""

    def __init__(self, exc: Exception) -> None:
        self.original = exc
        self.startup_message = f"{type(exc).__name__}: {exc}"
        super().__init__(self.startup_message)


class SubagentManager(SessionMixin):
    """Run external CLI subagents inside a task-scoped folder."""

    def __init__(self, root: Path, execution_root: Path) -> None:
        self.root = root.resolve()
        self.execution_root = execution_root.resolve()
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
        """Pick the stage label exported as ``LITEHIVE_STAGE`` to the subagent and used to choose its report bucket; falls back to a role-default when the task has no current stage yet."""
        current_stage = task.runtime.pipeline.current_stage.stage
        if current_stage:
            return current_stage
        if task.pipeline_status:
            pipeline_stage = str(task.pipeline_status)
        else:
            pipeline_stage = ""
        if (
            pipeline_stage in _REPORTABLE_STAGES
            or pipeline_stage == PipelineState.MERGE_RESOLVING
            or pipeline_stage == PipelineState.RECOVERING
        ):
            return pipeline_stage
        if role and role in _DEFAULT_STAGE_FOR_ROLE:
            return _DEFAULT_STAGE_FOR_ROLE[role]
        return TaskStage.IMPLEMENTING.value

    @classmethod
    def _report_stage_for_task(cls, task: TaskRecord, role: str | None = None) -> str:
        """Narrow the stage to one ``StageReport`` accepts (reportable stages plus merge-resolving/recovering); guards record_stage_report from non-reporting pseudo-stages."""
        stage = cls._agent_stage_for_task(task, role)
        if stage in _REPORTABLE_STAGES or stage == PipelineState.RECOVERING:
            return stage
        if stage == PipelineState.MERGE_RESOLVING:
            return PipelineState.MERGE_RESOLVING.value
        return TaskStage.IMPLEMENTING.value

    def run(
        self,
        task: TaskRecord,
        role: str,
        engine_name: str,
        prompt: str,
        model: str | None = None,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> SubagentResult:
        """Drive one subagent end-to-end (folder, sandbox, live callbacks, transcript, report); the lifecycle stage handlers call this once per subagent invocation."""
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
        self.write_session_start(task, base, ref, prompt)
        failure: EngineFailure | None = None
        callback_warnings: list[str] = []
        engine_started = False

        def _safe_on_started(pid: int) -> None:
            nonlocal engine_started
            engine_started = True
            try:
                self.record_subagent_pid(task, ref, pid)
            except Exception as exc:  # callback failures must not crash the runner
                self._record_live_callback_failure(
                    ref=ref,
                    phase="start",
                    exc=exc,
                    warnings=callback_warnings,
                )

        def _safe_on_update(execution: CLIExecutionResult) -> None:
            nonlocal engine_started
            if execution.pid is not None:
                engine_started = True
            try:
                self.write_session_progress(
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
            if not _check_engine_availability_with_retry(engine):
                raise EngineError(f"Engine '{engine.name}' is unavailable: missing binary '{engine.binary}'")
            if isinstance(engine, ExternalCLIAdapter) and sandbox_summary.enabled:
                execution_engine = SandboxedAdapter(engine, self.sandbox, engine_name, role)
            # Probe the wrapped adapter for capability preference. The sandbox wrapper
            # exposes both run and run_live, so inspecting the wrapper would hide
            # whether the underlying engine actually prefers a custom run override.
            if execution_engine is not engine:
                live_execution_probe = engine
            else:
                live_execution_probe = execution_engine
            callback_probe = live_execution_probe
            task_env = {
                "LITEHIVE_TASK_ID": task.id,
                "LITEHIVE_WORKSPACE_ROOT": str(self.root),
                "LITEHIVE_AGENT_ROLE": role,
                "LITEHIVE_SUBAGENT_ID": ref.id,
                "LITEHIVE_STAGE": self._agent_stage_for_task(task, role),
                "LITEHIVE_PYTHON_PATH": sys.executable,
            }
            effective_model = resume_safe_model_override(
                engine_name,
                model,
                resume_session_id=resume_session_id,
            )
            if supports_live_execution(live_execution_probe):
                run_live_callable = effective_engine_callable(execution_engine, "run_live")
                if not callable(run_live_callable):
                    run_live_callable = execution_engine.run_live
                inactivity_timeout_seconds = self.subagent_inactivity_timeout_seconds(engine_name)
                live_kwargs: dict[str, object] = {
                    "cwd": self.execution_root,
                    "model": effective_model,
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
                    "model": effective_model,
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
            completed_timeout = self.completed_inactivity_timeout(proc)
            if completed_timeout is not None:
                raise completed_timeout
            transcript = self.render_execution_trace(
                ref.engine,
                proc,
            )
            continuation = self.extract_execution_continuation(ref.engine, proc)
            if proc.exit_code == 0:
                ref.status = "completed"
            else:
                ref.status = "failed"
            if proc.exit_code != 0:
                interruption_reason = classify_execution_interruption(
                    transcript,
                    exit_code=proc.exit_code,
                )
                limit_reason = classify_execution_limit(transcript)
                retryable_failure = classify_retryable_execution_failure(transcript)
                if interruption_reason is not None:
                    ref.status = "interrupted"
                    failure = EngineFailure(
                        kind="execution_interrupted",
                        reason=interruption_reason,
                    )
                elif limit_reason is not None:
                    failure = EngineFailure(kind="execution_limit", reason=limit_reason)
                elif retryable_failure is not None:
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
            transcript = self.render_execution_trace(
                ref.engine,
                proc,
            )
            continuation = self.extract_execution_continuation(ref.engine, proc)
            ref.status = "failed"
            failure = EngineFailure(
                kind="retryable_execution_error",
                reason="transient timeout",
                classification="timeout",
            )
        except (EngineError, SandboxError) as exc:
            if not engine_started:
                raise SubagentStartupError(exc) from exc
            transcript = str(exc)
            proc = None
            continuation = None
            ref.status = "blocked"
            failure = EngineFailure(kind="engine_error", reason=str(exc))
        except Exception as exc:
            if not engine_started:
                raise SubagentStartupError(exc) from exc
            raise

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
            continuation=continuation,
            extra_warnings=callback_warnings,
        )
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
            execution_trace=transcript,
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
        continuation,
        extra_warnings: list[str],
    ) -> None:
        report_stage = self._report_stage_for_task(task, ref.role)
        report = self._parse_execution_report(
            task=task,
            stage=report_stage,
            ref=ref,
            execution=execution,
            transcript=transcript,
        )
        report = report.model_copy(update={"warnings": self._merged_warnings(report.warnings, extra_warnings)})
        files_changed = _latest_report_files_changed(
            self.root,
            task,
            str(report.pipeline_state),
            source_subagent_id=ref.id,
        )
        record_stage_report(self.root, task, report)
        self.write_session_snapshot(
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
                "files_changed": files_changed,
                "tests": report.tests,
                "warnings": report.warnings,
                "resource_control": self.sandbox.policy_summary(ref.engine, ref.role).as_dict(),
                "interruption_reason": interruption_reason,
                "continuation": None if continuation is None else continuation.model_dump(mode="python"),
            },
            exit_code=exit_code,
            pid=None if execution is None else execution.pid,
            interruption_reason=interruption_reason,
            continuation=continuation,
        )
        write_stream_artifact(base, "stdout", "" if execution is None else execution.stdout, compress=True)
        write_stream_artifact(base, "stderr", "" if execution is None else execution.stderr, compress=True)
        if execution is not None:
            self.append_stream_delta(base, ref, "stdout", execution.stdout)
            self.append_stream_delta(base, ref, "stderr", execution.stderr)
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
        self.write_event_stream(ref, task, "" if execution is None else execution.stdout)

    def write_session_progress(
        self,
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
        prompt: str,
        execution: CLIExecutionResult,
    ) -> None:
        """Live progress snapshot called from the engine's ``on_update`` callback; persists transcript/streams so an operator watching ``litehive status`` sees a running subagent's output before it exits."""
        engine = get_engine(ref.engine)
        transcript = self.render_execution_trace(
            ref.engine,
            execution,
        )
        continuation = self.extract_execution_continuation(ref.engine, execution)
        if isinstance(engine, ExternalCLIAdapter):
            record_engine_observation(
                self.root,
                task_id=task.id,
                engine_name=ref.engine,
                adapter=engine,
                execution=execution,
            )
        self.record_subagent_pid(task, ref, execution.pid)
        mark_subagent_progress(
            self.root,
            task,
            pid=execution.pid,
            transcript=transcript,
            continuation=continuation,
        )
        self.write_session_metadata(
            task,
            ref,
            exit_code=None,
            pid=execution.pid,
            interruption_reason=None,
            continuation=continuation,
        )
        write_text_if_changed(base / "prompt.txt", prompt)
        write_text_if_changed(base / "stdout.txt", execution.stdout)
        write_text_if_changed(base / "stderr.txt", execution.stderr)
        self.append_stream_delta(base, ref, "stdout", execution.stdout)
        self.append_stream_delta(base, ref, "stderr", execution.stderr)
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
                "files_changed": _latest_report_files_changed(
                    self.root,
                    task,
                    str(report.pipeline_state),
                    source_subagent_id=ref.id,
                ),
                "tests": report.tests,
                "warnings": report.warnings,
                "resource_control": self.sandbox.policy_summary(ref.engine, ref.role).as_dict(),
                "continuation": None if continuation is None else continuation.model_dump(mode="python"),
            }
        self.write_session_snapshot(
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
            continuation=continuation,
        )
        self.write_event_stream(ref, task, execution.stdout)
        self.check_stdout_inactivity(base, ref.engine, execution)

    def _parse_execution_report(
        self,
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
                execution_trace=transcript,
                exit_code=0 if execution is None else execution.exit_code,
            ),
            root=self.root,
        )
