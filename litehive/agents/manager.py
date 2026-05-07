"""SubagentManager: run external CLI subagents inside a task-scoped folder."""

from dataclasses import dataclass, replace
import logging
from pathlib import Path
import sys
import time
from typing import cast

from heru.adapters import (
    EngineError,
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
)
from heru.base import CLIExecutionResult, ExternalCLIAdapter
from heru.types import RuntimeEngineContinuation
from litehive.agents.callbacks import CallbackWarnings, SubagentRunCallbacks
from litehive.agents.engine_callables import resolve_cli_execution_callable
from litehive.agents.execution_trace import render_execution_trace
from litehive.sandbox.adapter import SandboxedAdapter
from litehive.sandbox.launcher import SandboxError, SandboxLauncher, SandboxPolicySummary
from litehive.config.model import LitehiveConfig
from litehive.agents.engine_manager import EngineManager
from litehive.domain.reports import REPORT_VERDICT_KINDS, ReportPipelineState, StageReport
from litehive.domain.task import TaskRecord
from litehive.observability.engine_monitoring import record_engine_execution, record_engine_observation
from litehive.agents.artifacts import (
    write_stream_artifact,
    write_text_if_changed,
)
from heru.engine_detection import (
    filter_supported_kwargs,
    supports_live_execution,
    supports_live_on_started,
    supports_on_started,
)
from litehive.domain.agent import EngineFailure, ExecutionTrace, SubagentId, SubagentInactivityTimeout, SubagentResult
from litehive.domain.common import SubagentStatus
from litehive.domain.runtime import Subagent
from litehive.agents.report_extraction import MissingVerdictError, stage_report_from_subagent
from litehive.agents.session import SubagentSessionManager
from litehive.agents.session_events import SubagentFinishedEvent, SubagentProgressEvent
from litehive.agents.session_continuation import subagent_continuation_state
from litehive.agents.session_reports import SubagentReportPayload
from litehive.agents.session_snapshots import (
    RunningSubagentSessionMetadata,
    SubagentSessionMetadata,
    SubagentSessionSnapshot,
)
from litehive.agents.subagent_ids import SubagentIdRepository
from litehive.domain.roles import agent_stage_for_task
from litehive.state.records import save_task
from litehive.tasks.activity_rendering import normalized_files_changed
from litehive.tasks.paths import task_dir
from litehive.tasks.report_storage import record_stage_report
from litehive.tasks.runtime import (
    mark_subagent_finished_for_workspace,
    mark_subagent_progress_for_workspace,
    mark_subagent_started_for_workspace,
)
from litehive.workspace import Workspace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubagentRunContext:
    """
    Prepared filesystem/task state for one subagent invocation.
    """

    base: Path
    ref: Subagent
    engine_adapter: object
    run_adapter: object
    sandbox_summary: SandboxPolicySummary
    callbacks: SubagentRunCallbacks


@dataclass(frozen=True)
class EngineProcessResult:
    """
    Adapter selected for execution plus the process result it returned.
    """

    execution: CLIExecutionResult
    run_adapter: object


@dataclass(frozen=True)
class EngineRunOutcome:
    """
    Result of invoking and classifying one engine process.
    """

    execution: CLIExecutionResult
    transcript: str
    continuation: RuntimeEngineContinuation | None
    failure: EngineFailure | None
    run_adapter: object


def _latest_report_files_changed(
    workspace: Workspace,
    task: TaskRecord,
    pipeline_state: ReportPipelineState,
    source_subagent_id: SubagentId,
) -> list[str]:
    """
    Read this subagent's most recent activity entry's normalized file list.

    Used by the finish/progress snapshots so they show *what this
    subagent actually touched*, not whatever the subagent
    self-reported in free-form text — agents sometimes hallucinate
    files they did not edit, and the activity entry is the
    canonical post-verdict record. The source subagent id is required
    because another subagent can report to the same stage later in the
    same task, and an unfiltered lookup would attribute its files to
    the wrong session.
    """
    latest = workspace.task_activity(task).latest_entry(
        stage=pipeline_state,
        source_subagent_id=source_subagent_id,
        verdicts=REPORT_VERDICT_KINDS,
    )
    if latest is None:
        return []
    return normalized_files_changed(latest.files_changed)


def _check_engine_availability_with_retry(engine, max_retries: int = 2, delay: float = 0.5) -> bool:
    """
    Probe engine availability with a small retry loop.

    Engine availability checks shell out and can flake on transient
    filesystem or PATH issues during subprocess execution; without
    the retry, a single hiccup would fail the task even though the
    engine is fine. The retry budget is kept small because real
    unavailability should fail quickly.
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
    """
    Launch-boundary failure raised by ``SubagentManager``.

    ``SubagentManager.run`` is the only production actor that raises
    this exception. It wraps failures that happen before the external
    engine process is known to have started: engine availability
    checks, sandbox adapter setup, and immediate adapter launch
    exceptions before ``on_started`` or live progress has confirmed a
    pid. Once the engine has started, manager code records the problem
    as an ``EngineFailure`` or lets the original exception propagate
    instead of calling it a startup failure.
    """

    def __init__(self, exc: Exception) -> None:
        """
        Wrap the original launch-time exception.

        ``HeruEngineAdapter._handle_startup_failure`` catches this
        wrapper. It needs both the underlying cause, for the
        no-recovery path that re-raises the original exception, and a
        pre-formatted ``startup_message`` for the direct-recovery
        prompt. Carrying both fields on the exception keeps the
        lifecycle handoff from losing either piece of information.
        """
        self.original = exc
        self.startup_message = f"{type(exc).__name__}: {exc}"
        super().__init__(self.startup_message)


class SubagentManager:
    """Run external CLI subagents inside a task-scoped folder."""

    def __init__(
        self,
        root: Path,
        execution_root: Path,
        *,
        workspace: Workspace,
        config: LitehiveConfig,
        sandbox: SandboxLauncher,
        sessions: SubagentSessionManager,
        engines: EngineManager,
        subagent_ids: SubagentIdRepository,
    ) -> None:
        """
        Bind the manager to a workspace plus an execution cwd.

        ``HeruEngineAdapter.run_turn`` constructs one ``SubagentManager``
        per agent turn so the cwd reflects the role-appropriate
        checkout, while the container injects the session collaborator
        that owns persistence for snapshots, streams, PID metadata, and
        inactivity checks.
        """
        self.root = root.resolve()
        self.execution_root = execution_root.resolve()
        self.workspace = workspace
        self.config = config
        self.sandbox = sandbox
        self.sessions = sessions
        self.engines = engines
        self.subagent_ids = subagent_ids

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
        """
        Drive one subagent invocation end-to-end.

        Allocates the subagent folder, applies the sandbox, wires the
        live callbacks, runs the engine, then renders the transcript
        and StageReport. Called once per subagent invocation by the
        lifecycle stage handlers; everything observability-related
        (events, snapshots, runtime state) flows through this method.
        """
        context = self._prepare_subagent_run(
            task=task,
            role=role,
            engine_name=engine_name,
            prompt=prompt,
        )
        outcome = self._execute_subagent_engine(
            task=task,
            context=context,
            engine_name=engine_name,
            role=role,
            prompt=prompt,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
        )
        return self._finalize_subagent_run(
            task=task,
            context=context,
            prompt=prompt,
            engine_name=engine_name,
            outcome=outcome,
        )

    def _prepare_subagent_run(
        self,
        task: TaskRecord,
        role: str,
        engine_name: str,
        prompt: str,
    ) -> SubagentRunContext:
        """
        Allocate the subagent record, artifact directory, and callbacks.
        """
        subagent_id = self.subagent_ids.reserve_next_id(task)
        folder_name = f"{subagent_id}-{role}"
        base = task_dir(self.root, task) / "subagents" / folder_name
        base.mkdir(parents=True, exist_ok=False)

        engine_adapter = self.engines.engine_for(engine_name)
        run_adapter = engine_adapter
        sandbox_summary = self.sandbox.policy_summary(engine_name)
        ref = Subagent(
            id=subagent_id,
            role=role,
            engine=engine_name,
            status=SubagentStatus.RUNNING.value,
            path=f"subagents/{folder_name}",
            sandboxed=sandbox_summary.enabled,
            sandbox_summary=sandbox_summary.summary,
        )
        task.subagents.append(ref)
        save_task(self.root, task)
        mark_subagent_started_for_workspace(self.workspace, task, ref)
        self.sessions.write_session_start(task, base, ref, prompt)
        callbacks = SubagentRunCallbacks(
            task=task,
            base=base,
            ref=ref,
            prompt=prompt,
            sessions=self.sessions,
            progress_writer=self,
        )
        return SubagentRunContext(
            base=base,
            ref=ref,
            engine_adapter=engine_adapter,
            run_adapter=run_adapter,
            sandbox_summary=sandbox_summary,
            callbacks=callbacks,
        )

    def _execute_subagent_engine(
        self,
        task: TaskRecord,
        context: SubagentRunContext,
        engine_name: str,
        role: str,
        prompt: str,
        model: str | None,
        max_turns: int | None,
        resume_session_id: str | None,
    ) -> EngineRunOutcome:
        """
        Invoke the engine adapter and classify its execution result.
        """
        ref = context.ref
        callbacks = context.callbacks
        run_adapter = context.run_adapter
        try:
            process = self._run_engine_process(
                task=task,
                context=context,
                engine_name=engine_name,
                role=role,
                prompt=prompt,
                model=model,
                max_turns=max_turns,
                resume_session_id=resume_session_id,
            )
            proc = process.execution
            run_adapter = process.run_adapter
            completed_timeout = self.sessions.completed_inactivity_timeout(proc)
            if completed_timeout is not None:
                raise completed_timeout
            transcript = render_execution_trace(proc)
            continuation = self.sessions.extract_execution_continuation(ref.engine, proc)
            failure = self._classify_completed_execution(ref, proc, transcript)
        except SubagentInactivityTimeout as exc:
            timeout_note = str(exc)
            stderr = exc.execution.stderr
            if timeout_note not in stderr:
                stderr = f"{stderr.rstrip()}\n{timeout_note}".strip()
            proc = replace(exc.execution, exit_code=124, stderr=stderr)
            transcript = render_execution_trace(proc)
            continuation = self.sessions.extract_execution_continuation(ref.engine, proc)
            ref.status = SubagentStatus.FAILED.value
            failure = EngineFailure(
                kind="retryable_execution_error",
                reason="transient timeout",
                classification="timeout",
            )
        except (EngineError, SandboxError) as exc:
            if not callbacks.engine_started:
                raise SubagentStartupError(exc) from exc
            raise
        except Exception as exc:
            if not callbacks.engine_started:
                raise SubagentStartupError(exc) from exc
            raise

        return EngineRunOutcome(
            execution=proc,
            transcript=transcript,
            continuation=continuation,
            failure=failure,
            run_adapter=run_adapter,
        )

    def _run_engine_process(
        self,
        task: TaskRecord,
        context: SubagentRunContext,
        engine_name: str,
        role: str,
        prompt: str,
        model: str | None,
        max_turns: int | None,
        resume_session_id: str | None,
    ) -> EngineProcessResult:
        """
        Select the adapter and call the engine-specific run method.

        The capability checks intentionally inspect the underlying
        engine adapter, not the sandbox wrapper. The wrapper exposes
        both methods, while the underlying adapter tells us whether
        this engine actually prefers live execution or a custom run
        override.
        """
        engine_adapter = cast(ExternalCLIAdapter, context.engine_adapter)
        run_adapter = context.run_adapter
        if not _check_engine_availability_with_retry(engine_adapter):
            raise EngineError(
                f"Engine '{engine_adapter.name}' is unavailable: missing binary '{engine_adapter.binary}'"
            )
        if isinstance(engine_adapter, ExternalCLIAdapter) and context.sandbox_summary.enabled:
            run_adapter = SandboxedAdapter(engine_adapter, self.sandbox, engine_name, role)

        task_env = {
            "LITEHIVE_TASK_ID": task.id,
            "LITEHIVE_WORKSPACE_ROOT": str(self.root),
            "LITEHIVE_AGENT_ROLE": role,
            "LITEHIVE_SUBAGENT_ID": context.ref.id,
            "LITEHIVE_STAGE": agent_stage_for_task(task, role).value,
            "LITEHIVE_PYTHON_PATH": sys.executable,
        }
        effective_model = self.engines.resume_safe_model(
            engine_name,
            model,
            resume_session_id,
        )
        if supports_live_execution(engine_adapter):
            execution = self._run_live_engine_process(
                context=context,
                run_adapter=run_adapter,
                engine_name=engine_name,
                prompt=prompt,
                task_env=task_env,
                effective_model=effective_model,
                max_turns=max_turns,
                resume_session_id=resume_session_id,
            )
        else:
            execution = self._run_single_engine_process(
                context=context,
                run_adapter=run_adapter,
                prompt=prompt,
                task_env=task_env,
                effective_model=effective_model,
                max_turns=max_turns,
                resume_session_id=resume_session_id,
            )
        return EngineProcessResult(execution=execution, run_adapter=run_adapter)

    def _run_live_engine_process(
        self,
        context: SubagentRunContext,
        run_adapter: object,
        engine_name: str,
        prompt: str,
        task_env: dict[str, str],
        effective_model: str | None,
        max_turns: int | None,
        resume_session_id: str | None,
    ) -> CLIExecutionResult:
        """
        Call an adapter's live execution entry point.
        """
        engine_adapter = context.engine_adapter
        run_live_callable = resolve_cli_execution_callable(run_adapter, "run_live")
        inactivity_timeout_seconds = self.sessions.subagent_inactivity_timeout_seconds(engine_name)
        live_kwargs: dict[str, object] = {
            "cwd": self.execution_root,
            "model": effective_model,
            "emit_unified": True,
            "extra_env": task_env,
            "on_update": context.callbacks.on_update,
        }
        if resume_session_id:
            live_kwargs["resume_session_id"] = resume_session_id
        if supports_live_on_started(engine_adapter):
            live_kwargs["on_started"] = context.callbacks.on_started
        if max_turns is not None:
            live_kwargs["max_turns"] = max_turns
        if inactivity_timeout_seconds > 0:
            live_kwargs["inactivity_timeout_seconds"] = inactivity_timeout_seconds
        return run_live_callable(
            prompt,
            **filter_supported_kwargs(run_live_callable, live_kwargs),
        )

    def _run_single_engine_process(
        self,
        context: SubagentRunContext,
        run_adapter: object,
        prompt: str,
        task_env: dict[str, str],
        effective_model: str | None,
        max_turns: int | None,
        resume_session_id: str | None,
    ) -> CLIExecutionResult:
        """
        Call an adapter's non-live execution entry point.
        """
        engine_adapter = context.engine_adapter
        run_callable = resolve_cli_execution_callable(run_adapter, "run")
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
        if supports_on_started(engine_adapter):
            run_kwargs["on_started"] = context.callbacks.on_started
        return run_callable(
            prompt,
            **filter_supported_kwargs(run_callable, run_kwargs),
        )

    def _classify_completed_execution(
        self,
        ref: Subagent,
        proc: CLIExecutionResult,
        transcript: str,
    ) -> EngineFailure | None:
        """
        Update terminal status and classify non-zero exits.
        """
        if proc.exit_code == 0:
            ref.status = SubagentStatus.COMPLETED.value
            return None

        ref.status = SubagentStatus.FAILED.value
        interruption_reason = classify_execution_interruption(
            transcript,
            exit_code=proc.exit_code,
        )
        if interruption_reason is not None:
            ref.status = SubagentStatus.INTERRUPTED.value
            return EngineFailure(
                kind="execution_interrupted",
                reason=interruption_reason,
            )

        limit_reason = classify_execution_limit(transcript)
        if limit_reason is not None:
            return EngineFailure(kind="execution_limit", reason=limit_reason)

        retryable_failure = classify_retryable_execution_failure(transcript)
        if retryable_failure is None:
            return None
        return EngineFailure(
            kind="retryable_execution_error",
            reason=retryable_failure.reason,
            classification=retryable_failure.classification,
        )

    def _finalize_subagent_run(
        self,
        task: TaskRecord,
        context: SubagentRunContext,
        prompt: str,
        engine_name: str,
        outcome: EngineRunOutcome,
    ) -> SubagentResult:
        """
        Persist terminal runtime/session state and return the run result.
        """
        ref = context.ref
        base = context.base
        proc = outcome.execution
        transcript = outcome.transcript
        continuation = outcome.continuation
        failure = outcome.failure

        save_task(self.root, task)
        proc_exit_code = proc.exit_code
        proc_pid = proc.pid
        if failure is None or failure.kind != "execution_interrupted":
            interruption_reason = None
        else:
            interruption_reason = failure.reason
        if failure is None:
            failure_kind = None
            failure_reason = None
        else:
            failure_kind = failure.kind
            failure_reason = failure.reason
        mark_subagent_finished_for_workspace(
            self.workspace,
            task,
            ref,
            transcript,
            proc_exit_code,
            pid=proc_pid,
            interruption_reason=interruption_reason,
            continuation=continuation,
        )
        self._write_session_finish(
            task,
            base,
            ref,
            prompt,
            transcript,
            proc_exit_code,
            proc,
            interruption_reason=interruption_reason,
            continuation=continuation,
            callback_warnings=context.callbacks.warnings,
        )
        record_engine_execution(
            self.workspace,
            task_id=task.id,
            engine_name=engine_name,
            adapter=cast(ExternalCLIAdapter, outcome.run_adapter),
            execution=proc,
            failure_kind=failure_kind,
            failure_reason=failure_reason,
        )
        return SubagentResult(
            ref=ref,
            execution=proc,
            execution_trace=ExecutionTrace.from_text(transcript),
            exit_code=proc_exit_code,
            failure=failure,
            continuation=continuation,
        )

    def _write_session_finish(
        self,
        task: TaskRecord,
        base: Path,
        ref: Subagent,
        prompt: str,
        transcript: str,
        exit_code: int,
        execution: CLIExecutionResult,
        interruption_reason: str | None,
        continuation,
        callback_warnings: CallbackWarnings,
    ) -> None:
        """
        End-of-run snapshot writer called once from ``run``.

        Persists the parsed StageReport, snapshot files, stream
        artifacts, and the ``subagent_finished`` event in one
        sweep so downstream readers — the lifecycle verdict reader,
        the status display, recovery diagnostics — all see a single
        consistent terminal record for this subagent.
        """
        report_stage = agent_stage_for_task(task, ref.role)
        report = self._parse_execution_report(
            task=task,
            stage=report_stage,
            ref=ref,
            execution=execution,
            transcript=transcript,
        )
        execution_stdout = execution.stdout
        execution_stderr = execution.stderr
        execution_pid = execution.pid
        continuation_state = subagent_continuation_state(continuation)
        if report is None:
            # Agent finished without submitting a verdict. We do NOT call
            # record_stage_report here — recording a synthetic "reject" would
            # lie about what happened. The lifecycle's NudgeRequired path
            # will reissue the turn and the next run will produce a real
            # verdict (or exhaust the nudge budget and crash). The snapshot
            # still records the run for observability with merged warnings
            # so an operator watching `litehive status` can see why no
            # report was written.
            missing_verdict_warning = (
                "Agent did not submit verdict via `litehive agent report` CLI; "
                "lifecycle will nudge the agent."
            )
            warnings = callback_warnings.merged_with([missing_verdict_warning])
            report_payload = SubagentReportPayload(
                status=SubagentStatus(ref.status),
                summary=f"{report_stage}: agent did not submit verdict via litehive agent report CLI",
                tests={"added": 0, "passing": 0},
                warnings=warnings,
                resource_control=self.sandbox.policy_summary(ref.engine),
                interruption_reason=interruption_reason,
                continuation=continuation_state,
            )
        else:
            report = report.model_copy(update={"warnings": callback_warnings.merged_with(report.warnings)})
            files_changed = _latest_report_files_changed(
                self.workspace,
                task,
                report.pipeline_state,
                source_subagent_id=SubagentId(ref.id),
            )
            record_stage_report(self.workspace, task, report)
            report_payload = SubagentReportPayload(
                status=SubagentStatus(ref.status),
                summary=report.summary,
                files_changed=files_changed,
                tests=report.tests,
                warnings=report.warnings,
                resource_control=self.sandbox.policy_summary(ref.engine),
                interruption_reason=interruption_reason,
                continuation=continuation_state,
            )
        self.sessions.write_session_snapshot(
            task,
            base,
            ref,
            snapshot=SubagentSessionSnapshot(
                prompt=prompt,
                transcript=transcript + "\n",
                stdout=execution_stdout,
                stderr=execution_stderr,
                report=report_payload,
                metadata=SubagentSessionMetadata(
                    exit_code=exit_code,
                    pid=execution_pid,
                    interruption_reason=interruption_reason,
                    continuation=continuation_state,
                ),
            ),
        )
        write_stream_artifact(base, "stdout", execution_stdout, compress=True)
        write_stream_artifact(base, "stderr", execution_stderr, compress=True)
        self.sessions.append_stream_delta(base, ref, "stdout", execution.stdout)
        self.sessions.append_stream_delta(base, ref, "stderr", execution.stderr)
        self.workspace.append_event(
            task,
            SubagentFinishedEvent(
                subagent_id=ref.id,
                role=ref.role,
                engine=ref.engine,
                status=SubagentStatus(ref.status),
                exit_code=exit_code,
                interruption_reason=interruption_reason,
            ),
        )
        self.sessions.write_event_stream(ref, task, execution_stdout)

    def write_session_progress(
        self,
        task: TaskRecord,
        base: Path,
        ref: Subagent,
        prompt: str,
        execution: CLIExecutionResult,
    ) -> None:
        """
        Live progress snapshot called from ``on_update``.

        Persists transcript and stream artifacts so an operator
        watching ``litehive status`` sees a running subagent's output
        before it exits; without this path, the CLI only ever sees
        output after the engine finishes.
        """
        engine = self.engines.engine_for(ref.engine)
        transcript = render_execution_trace(execution)
        continuation = self.sessions.extract_execution_continuation(ref.engine, execution)
        continuation_state = subagent_continuation_state(continuation)
        if isinstance(engine, ExternalCLIAdapter):
            record_engine_observation(
                self.workspace,
                task_id=task.id,
                engine_name=ref.engine,
                adapter=engine,
                execution=execution,
            )
        self.sessions.record_subagent_pid(task, ref, execution.pid)
        mark_subagent_progress_for_workspace(
            self.workspace,
            task,
            pid=execution.pid,
            transcript=transcript,
            continuation=continuation,
        )
        self.sessions.write_running_session_metadata(
            task,
            ref,
            metadata=RunningSubagentSessionMetadata(
                pid=execution.pid,
                continuation=continuation_state,
            ),
        )
        write_text_if_changed(base / "prompt.txt", prompt)
        write_text_if_changed(base / "stdout.txt", execution.stdout)
        write_text_if_changed(base / "stderr.txt", execution.stderr)
        self.sessions.append_stream_delta(base, ref, "stdout", execution.stdout)
        self.sessions.append_stream_delta(base, ref, "stderr", execution.stderr)
        self.workspace.append_event(
            task,
            SubagentProgressEvent(subagent_id=ref.id, role=ref.role, pid=execution.pid),
        )
        report_stage = agent_stage_for_task(task, ref.role)
        report_payload = SubagentReportPayload(
            status=SubagentStatus(ref.status),
            summary="",
            tests={"added": 0, "passing": 0},
            resource_control=self.sandbox.policy_summary(ref.engine),
        )
        if transcript.strip():
            report = self._parse_execution_report(
                task=task,
                stage=report_stage,
                ref=ref,
                execution=execution,
                transcript=transcript,
            )
            if report is None:
                # Live progress: the running agent has not yet called
                # `litehive agent report`. That's expected mid-turn — we
                # surface a clear placeholder summary so an operator
                # watching status can tell the agent is mid-flight, but
                # we do not record a stage-report row.
                report_payload = SubagentReportPayload(
                    status=SubagentStatus(ref.status),
                    summary=f"{report_stage}: agent did not submit verdict via litehive agent report CLI",
                    tests={"added": 0, "passing": 0},
                    resource_control=self.sandbox.policy_summary(ref.engine),
                    continuation=continuation_state,
                )
            else:
                report_payload = SubagentReportPayload(
                    status=SubagentStatus(ref.status),
                    summary=report.summary,
                    files_changed=_latest_report_files_changed(
                        self.workspace,
                        task,
                        report.pipeline_state,
                        source_subagent_id=SubagentId(ref.id),
                    ),
                    tests=report.tests,
                    warnings=report.warnings,
                    resource_control=self.sandbox.policy_summary(ref.engine),
                    continuation=continuation_state,
                )
        self.sessions.write_session_snapshot(
            task,
            base,
            ref,
            snapshot=SubagentSessionSnapshot(
                prompt=prompt,
                transcript=transcript,
                stdout=execution.stdout,
                stderr=execution.stderr,
                report=report_payload,
                metadata=SubagentSessionMetadata(
                    exit_code=None,
                    pid=execution.pid,
                    continuation=continuation_state,
                ),
            ),
        )
        self.sessions.write_event_stream(ref, task, execution.stdout)
        self.sessions.check_stdout_inactivity(base, ref.engine, execution)

    def _parse_execution_report(
        self,
        task: TaskRecord,
        stage: ReportPipelineState,
        ref: Subagent,
        execution: CLIExecutionResult,
        transcript: str,
    ) -> StageReport | None:
        """
        Construct a ``StageReport`` from the engine's transcript.

        Returns ``None`` when the agent finished without submitting a
        verdict via ``litehive agent report``: the runner should skip
        recording a stage-report row in that case (the lifecycle's
        ``NudgeRequired`` path produces the real verdict on the next
        turn). Both ``_write_session_finish`` and
        ``write_session_progress`` route through this single helper so
        the live-progress and finish paths produce reports of the same
        shape — without one helper, the live snapshot and the final
        snapshot could drift
        apart on field naming.
        """
        try:
            return stage_report_from_subagent(
                task,
                stage,
                SubagentResult(
                    ref=ref,
                    execution=execution,
                    execution_trace=ExecutionTrace.from_text(transcript),
                    exit_code=execution.exit_code,
                ),
                workspace=self.workspace,
            )
        except MissingVerdictError:
            # Agent finished without calling `litehive agent report`. The
            # lifecycle layer will raise NudgeRequired and re-issue the turn;
            # no stage-report row is written for this snapshot.
            return None
