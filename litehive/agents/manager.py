"""SubagentManager: run external CLI subagents inside a task-scoped folder."""

from dataclasses import replace
import logging
from pathlib import Path
import re
import sys
import time
from typing import Callable, cast

from heru import get_engine, resume_safe_model_override
from heru.adapters import (
    EngineError,
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
)
from heru.base import CLIExecutionResult, ExternalCLIAdapter
from litehive.agents.sandbox import SandboxError, SandboxLauncher
from litehive.config.model import LitehiveConfig
from litehive.observability.events import append_event
from heru.types import SubagentRef
from litehive.domain.reports import REPORT_VERDICT_KINDS, ReportPipelineState, StageReport
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
from litehive.agents.parsing import MissingVerdictError, stage_report_from_subagent
from litehive.agents.sandbox import SandboxedAdapter
from litehive.agents.session import SessionMixin
from litehive.state.records import save_task
from litehive.tasks.paths import task_dir
from litehive.tasks.runtime import (
    mark_subagent_finished,
    mark_subagent_progress,
    mark_subagent_started,
)
from litehive.domain.common import PipelineState, TaskStage, task_stage_for_pipeline_state
from litehive.tasks.activity import latest_task_activity_entry
from litehive.tasks.activity_rendering import normalized_files_changed
from litehive.tasks.report_storage import record_stage_report
from litehive.workspace import Workspace

_REPORTABLE_STAGES: frozenset[TaskStage] = frozenset(TaskStage)
_DEFAULT_STAGE_FOR_ROLE: dict[str, TaskStage | PipelineState] = {
    "planner": TaskStage.GROOMING,
    "swe": TaskStage.IMPLEMENTING,
    "qa": TaskStage.TESTING,
    "reviewer": TaskStage.ACCEPTING,
    "merge-resolver": PipelineState.MERGE_RESOLVING,
    "recovery": PipelineState.RECOVERING,
}

logger = logging.getLogger(__name__)


def _latest_report_files_changed(
    workspace: Workspace,
    task: TaskRecord,
    pipeline_state: ReportPipelineState,
    source_subagent_id: str | None = None,
) -> list[str]:
    """
    Read the most recent activity entry's normalized file list.

    Used by the finish/progress snapshots so they show *what this
    subagent actually touched*, not whatever the subagent
    self-reported in free-form text — agents sometimes hallucinate
    files they did not edit, and the activity entry is the
    canonical post-verdict record.
    """
    latest = latest_task_activity_entry(
        workspace,
        task,
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
    """Unexpected failure before the engine subprocess started."""

    def __init__(self, exc: Exception) -> None:
        """
        Wrap the original launch-time exception.

        The caller (``HeruEngineAdapter._handle_startup_failure``)
        needs both the underlying cause (to re-raise on the
        no-recovery path) and a pre-formatted ``startup_message`` for
        the recovery prompt — carrying both on the exception means it
        cannot lose either piece of information mid-handoff.
        """
        self.original = exc
        self.startup_message = f"{type(exc).__name__}: {exc}"
        super().__init__(self.startup_message)


class SubagentManager(SessionMixin):
    """Run external CLI subagents inside a task-scoped folder."""

    def __init__(
        self,
        root: Path,
        execution_root: Path,
        *,
        workspace: Workspace,
        config: LitehiveConfig,
        sandbox: SandboxLauncher,
    ) -> None:
        """
        Bind the manager to a workspace plus an execution cwd.

        ``HeruEngineAdapter.run_turn`` constructs one ``SubagentManager``
        per agent turn so the cwd reflects the role-appropriate
        checkout — task worktree for SWE/QA, litehive source tree for
        recovery — without needing to mutate a shared instance.
        """
        self.root = root.resolve()
        self.execution_root = execution_root.resolve()
        self.workspace = workspace
        self.config = config
        self.sandbox = sandbox
        self._stream_offsets: dict[str, int] = {}

    @staticmethod
    def _merged_warnings(base: list[str], extra: list[str]) -> list[str]:
        """
        Append ``extra`` warnings onto ``base`` while deduping by
        string equality.

        ``_write_session_finish`` uses this so callback warnings
        (live-update bookkeeping failures) are appended onto the
        parsed StageReport's own warnings without duplicating
        identical lines from a previous progress snapshot.
        """
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
        """
        Trap an exception from the engine's ``on_started`` /
        ``on_update`` callbacks.

        Turns it into a non-fatal warning carried on the eventual
        StageReport so a transient bookkeeping error (SQLite write
        race, filesystem hiccup) cannot kill the running subagent
        process — the engine is still doing real work, we just lost
        a snapshot.
        """
        warning = f"runner {phase} bookkeeping failed: {type(exc).__name__}: {exc}"
        if warning not in warnings:
            warnings.append(warning)
        logger.exception(
            "Subagent %s %s callback failed; continuing without crashing the runner",
            ref.id,
            phase,
        )

    @staticmethod
    def _agent_stage_for_task(task: TaskRecord, role: str | None = None) -> TaskStage | PipelineState:
        """
        Pick the stage label exported to the subagent.

        The label is what shows up as ``LITEHIVE_STAGE`` in the
        subagent's environment and what the activity-feed reader uses
        to bucket the agent's report; falls back to a role-default
        when the task has no current stage yet so an agent invoked
        before the runtime was wired still sees a sensible stage.

        Returns a domain enum member — either a :class:`TaskStage` for
        the five reportable stages or :data:`PipelineState.RECOVERING`
        / :data:`PipelineState.MERGE_RESOLVING` for the two pseudo-stages
        that can carry an agent verdict. Callers serialize to a string
        only at the env-var boundary.
        """
        current_stage = task.runtime.pipeline.current_stage.stage
        if current_stage:
            try:
                pipeline_state = PipelineState(current_stage)
            except ValueError:
                pipeline_state = None
            if pipeline_state is PipelineState.RECOVERING or pipeline_state is PipelineState.MERGE_RESOLVING:
                return pipeline_state
            if pipeline_state is not None:
                task_stage = task_stage_for_pipeline_state(pipeline_state)
                if task_stage is not None:
                    return task_stage
            try:
                return TaskStage(current_stage)
            except ValueError:
                pass
        if task.pipeline_status:
            try:
                pipeline_status_stage = TaskStage(task.pipeline_status.value)
            except ValueError:
                pipeline_status_stage = None
            if pipeline_status_stage is not None and pipeline_status_stage in _REPORTABLE_STAGES:
                return pipeline_status_stage
        if role and role in _DEFAULT_STAGE_FOR_ROLE:
            return _DEFAULT_STAGE_FOR_ROLE[role]
        return TaskStage.IMPLEMENTING

    @classmethod
    def _report_stage_for_task(cls, task: TaskRecord, role: str | None = None) -> ReportPipelineState:
        """
        Narrow the agent stage to one ``StageReport`` accepts.

        ``StageReport`` only stores reportable stages plus
        merge-resolving and recovering; this guards ``record_stage_report``
        from non-reporting pseudo-stages so a hook phase or worktree
        sync stage cannot accidentally land in the report storage.
        """
        stage = cls._agent_stage_for_task(task, role)
        if stage is PipelineState.RECOVERING:
            return PipelineState.RECOVERING
        if stage is PipelineState.MERGE_RESOLVING:
            return PipelineState.MERGE_RESOLVING
        if isinstance(stage, TaskStage):
            return stage
        return TaskStage.IMPLEMENTING

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
            """
            Engine ``on_started`` callback.

            Records the subagent pid so the runner can kill it on
            abort, and flips ``engine_started`` so a later failure is
            no longer treated as a startup error — once the engine
            is running, an exception is the engine's fault, not the
            launch's.
            """
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
            """
            Engine ``on_update`` callback.

            Persists a live progress snapshot so an operator watching
            ``litehive status`` sees the running subagent's transcript
            before the process exits — without these snapshots, the
            CLI would only show output once the engine finished.
            """
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
                "LITEHIVE_STAGE": self._agent_stage_for_task(task, role).value,
                "LITEHIVE_PYTHON_PATH": sys.executable,
            }
            effective_model = resume_safe_model_override(
                engine_name,
                model,
                resume_session_id=resume_session_id,
            )
            if supports_live_execution(live_execution_probe):
                run_live_callable = cast(
                    Callable[..., CLIExecutionResult],
                    effective_engine_callable(execution_engine, "run_live") or execution_engine.run_live,
                )
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
                run_callable = cast(
                    Callable[..., CLIExecutionResult],
                    effective_engine_callable(execution_engine, "run") or execution_engine.run,
                )
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
        if proc is None:
            proc_exit_code = 0
            proc_pid = None
        else:
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
        mark_subagent_finished(
            self.root,
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
            extra_warnings=callback_warnings,
        )
        if proc is not None:
            record_engine_execution(
                self.workspace,
                task_id=task.id,
                engine_name=engine_name,
                adapter=execution_engine,
                execution=proc,
                failure_kind=failure_kind,
                failure_reason=failure_reason,
            )
        return SubagentResult(
            ref=ref,
            execution=proc,
            execution_trace=transcript,
            exit_code=proc_exit_code,
            failure=failure,
            continuation=continuation,
        )

    def _next_subagent_id(self, task: TaskRecord) -> str:
        """
        Allocate the next ``SA-NNNN`` id for this task.

        Maxes existing in-memory refs against on-disk subagent
        folders so a previously-aborted run that left a directory
        behind without updating the task record cannot collide with
        the new id; without the disk-side max, a crashed launch could
        produce two subagents with the same id and overwrite each
        other's artifacts.
        """
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
        """
        End-of-run snapshot writer called once from ``run``.

        Persists the parsed StageReport, snapshot files, stream
        artifacts, and the ``subagent_finished`` event in one
        sweep so downstream readers — the lifecycle verdict reader,
        the status display, recovery diagnostics — all see a single
        consistent terminal record for this subagent.
        """
        report_stage = self._report_stage_for_task(task, ref.role)
        report = self._parse_execution_report(
            task=task,
            stage=report_stage,
            ref=ref,
            execution=execution,
            transcript=transcript,
        )
        if execution is None:
            execution_stdout = ""
            execution_stderr = ""
            execution_pid = None
        else:
            execution_stdout = execution.stdout
            execution_stderr = execution.stderr
            execution_pid = execution.pid
        if continuation is None:
            continuation_payload = None
        else:
            continuation_payload = continuation.model_dump(mode="python")
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
            warnings = self._merged_warnings([missing_verdict_warning], extra_warnings)
            report_payload = {
                "status": ref.status,
                "summary": (
                    f"{report_stage}: agent did not submit verdict via litehive agent report CLI"
                ),
                "files_changed": [],
                "tests": {"added": 0, "passing": 0},
                "warnings": warnings,
                "resource_control": self.sandbox.policy_summary(ref.engine, ref.role).as_dict(),
                "interruption_reason": interruption_reason,
                "continuation": continuation_payload,
            }
        else:
            report = report.model_copy(
                update={"warnings": self._merged_warnings(report.warnings, extra_warnings)}
            )
            files_changed = _latest_report_files_changed(
                self.workspace,
                task,
                report.pipeline_state,
                source_subagent_id=ref.id,
            )
            record_stage_report(self.workspace, task, report)
            report_payload = {
                "status": ref.status,
                "summary": report.summary,
                "files_changed": files_changed,
                "tests": report.tests,
                "warnings": report.warnings,
                "resource_control": self.sandbox.policy_summary(ref.engine, ref.role).as_dict(),
                "interruption_reason": interruption_reason,
                "continuation": continuation_payload,
            }
        self.write_session_snapshot(
            task,
            base,
            ref,
            prompt=prompt,
            transcript=transcript + "\n",
            stdout=execution_stdout,
            stderr=execution_stderr,
            report_payload=report_payload,
            exit_code=exit_code,
            pid=execution_pid,
            interruption_reason=interruption_reason,
            continuation=continuation,
        )
        write_stream_artifact(base, "stdout", execution_stdout, compress=True)
        write_stream_artifact(base, "stderr", execution_stderr, compress=True)
        if execution is not None:
            self.append_stream_delta(base, ref, "stdout", execution.stdout)
            self.append_stream_delta(base, ref, "stderr", execution.stderr)
        append_event(
            self.workspace,
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
        self.write_event_stream(ref, task, execution_stdout)

    def write_session_progress(
        self,
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
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
        engine = get_engine(ref.engine)
        transcript = self.render_execution_trace(
            ref.engine,
            execution,
        )
        continuation = self.extract_execution_continuation(ref.engine, execution)
        if isinstance(engine, ExternalCLIAdapter):
            record_engine_observation(
                self.workspace,
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
            self.workspace,
            task,
            "subagent_progress",
            data={
                "subagent_id": ref.id,
                "pid": execution.pid,
            },
        )
        report_stage = self._report_stage_for_task(task, ref.role)
        report_payload: dict[str, object] = {
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
            if continuation is None:
                continuation_payload = None
            else:
                continuation_payload = continuation.model_dump(mode="python")
            if report is None:
                # Live progress: the running agent has not yet called
                # `litehive agent report`. That's expected mid-turn — we
                # surface a clear placeholder summary so an operator
                # watching status can tell the agent is mid-flight, but
                # we do not record a stage-report row.
                report_payload = {
                    "status": ref.status,
                    "summary": (
                        f"{report_stage}: agent did not submit verdict via litehive agent report CLI"
                    ),
                    "files_changed": [],
                    "tests": {"added": 0, "passing": 0},
                    "warnings": [],
                    "resource_control": self.sandbox.policy_summary(ref.engine, ref.role).as_dict(),
                    "continuation": continuation_payload,
                }
            else:
                report_payload = {
                    "status": ref.status,
                    "summary": report.summary,
                    "files_changed": _latest_report_files_changed(
                        self.workspace,
                        task,
                        report.pipeline_state,
                        source_subagent_id=ref.id,
                    ),
                    "tests": report.tests,
                    "warnings": report.warnings,
                    "resource_control": self.sandbox.policy_summary(ref.engine, ref.role).as_dict(),
                    "continuation": continuation_payload,
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
        stage: ReportPipelineState,
        ref: SubagentRef,
        execution: CLIExecutionResult | None,
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
        if execution is None:
            execution_exit_code = 0
        else:
            execution_exit_code = execution.exit_code
        try:
            return stage_report_from_subagent(
                task,
                stage,
                SubagentResult(
                    ref=ref,
                    execution=execution,
                    execution_trace=transcript,
                    exit_code=execution_exit_code,
                ),
                workspace=self.workspace,
            )
        except MissingVerdictError:
            # Agent finished without calling `litehive agent report`. The
            # lifecycle layer will raise NudgeRequired and re-issue the turn;
            # no stage-report row is written for this snapshot.
            return None
