"""Core task execution runner."""

from dataclasses import dataclass
from pathlib import Path
import traceback
from typing import Protocol

import yaml

from litehive.models import (
    OutcomeKind,
    OutcomeReasonCode,
    RecoveryAction,
    RuntimeContinuationHandoff,
    StageReport,
    TaskRecord,
)
from litehive.tasks import (
    CLOSED_TASK_STATUSES,
    _apply_task_retry_state,
    _apply_stage_finished,
    _apply_task_outcome,
    _clear_task_outcome,
    _prepare_interrupted_task,
    append_journal,
    clear_task_outcome,
    create_follow_up_tasks,
    finish_task_run_transition,
    interruption_journal_message,
    mark_stage_finished,
    mark_stage_started,
    missing_acceptance_criteria_reason,
    needs_normalization,
    record_recovery_report,
    save_task,
    set_task_retry_state,
    task_dir,
)
from litehive.runner.states import _ROUTES, _SINGLE_ROUTES, _SINGLE_STEPS_FROM, _STEPS_FROM


class StageExecutor(Protocol):
    def __call__(self, task: TaskRecord, step: str) -> StageReport: ...


@dataclass(slots=True)
class RunResult:
    final_status: str
    steps_executed: int = 0
    last_verdict: str | None = None


class TaskExecutionRunner:
    """Drive one task through the fixed pipeline using a deterministic router.

    The runner uses the transition table in ``litehive.runner.states._ROUTES`` to
    decide the next stage after each executor call.  All routing is local and
    deterministic; the executor (a subagent or test stub) only produces reports.
    """

    def __init__(
        self,
        root: Path,
        executor: StageExecutor,
        max_retries: int = 3,
        retry_source: str = "global",
        stage_retry_limit: int = 2,
        subagents=None,
        config=None,
    ) -> None:
        self.root = root
        self.executor = executor
        self.max_retries = max_retries
        self.retry_source = retry_source
        self.stage_retry_limit = stage_retry_limit
        self.subagents = subagents
        self.config = config

    def run(self, task: TaskRecord) -> RunResult:
        steps = 0
        rejections = task.runtime.retry_count
        last_verdict: str | None = None
        single_mode = getattr(task, "pipeline_mode", "full") == "single"
        steps_from = _SINGLE_STEPS_FROM if single_mode else _STEPS_FROM
        routes = _SINGLE_ROUTES if single_mode else _ROUTES

        if task.pipeline_status == "done":
            return RunResult(final_status="done")

        if not single_mode:
            normalization_reason = needs_normalization(task)
            if normalization_reason is not None:
                task.pipeline_status = "grooming"  # type: ignore[assignment]
                save_task(self.root, task)
                append_journal(
                    self.root,
                    task,
                    f"Rerouted to grooming for normalization: {normalization_reason}",
                )

        set_task_retry_state(
            self.root,
            task,
            retry_count=rejections,
            retry_limit=self.max_retries,
            retry_source=self.retry_source,
        )
        clear_task_outcome(self.root, task)

        while True:
            current = steps_from.get(task.pipeline_status)
            if current is None:
                return self._finish_run(
                    task,
                    final_status=task.status,
                    steps=steps,
                    last_verdict=last_verdict,
                )

            missing_criteria_reason = (
                missing_acceptance_criteria_reason(task)
                if not single_mode and current in {"implementing", "testing", "accepting", "commit_to_git"}
                else None
            )
            if missing_criteria_reason is not None:
                report = self._terminal_report(
                    task,
                    step=current,
                    verdict="blocked",
                    summary=missing_criteria_reason,
                    outcome="blocked",
                    outcome_reason_code="missing_acceptance_criteria",
                    reason=missing_criteria_reason,
                    retry_count=rejections,
                )
                task.status = "flagged"
                _apply_task_outcome(
                    task,
                    kind="blocked",
                    stage=current,
                    reason_code="missing_acceptance_criteria",
                    reason=missing_criteria_reason,
                    retry_count=rejections,
                    retry_limit=self.max_retries,
                    retry_source=self.retry_source,
                )
                self._write_report(task, report, steps + 1)
                _apply_stage_finished(task, report)
                return self._finish_run(
                    task,
                    final_status="flagged",
                    steps=steps + 1,
                    last_verdict="blocked",
                )

            task.status = "in_progress"
            task.pipeline_status = current  # type: ignore[assignment]
            save_task(self.root, task)
            mark_stage_started(self.root, task, current)

            try:
                report = self.executor(task, current)
            except KeyboardInterrupt:
                reason = f"Execution interrupted during {current}"
                task.pipeline_status = current  # type: ignore[assignment]
                _prepare_interrupted_task(self.root, task, stage=current, summary=reason)
                if current == "commit_to_git":
                    task.status = "queued"
                append_journal(self.root, task, interruption_journal_message(task))
                report = self._terminal_report(
                    task,
                    step=current,
                    verdict="blocked",
                    summary=reason,
                    outcome="interrupted",
                    outcome_reason_code="execution_interrupted",
                    reason=reason,
                    retry_count=rejections,
                )
                self._write_report(task, report, steps + 1)
                return self._finish_run(
                    task,
                    final_status="interrupted",
                    steps=steps + 1,
                    last_verdict="blocked",
                )
            except Exception as exc:
                reason = f"{current} failed with unhandled error: {exc}"
                traceback_text = traceback.format_exc()
                # Try recovery agent before flagging
                if self.subagents is not None:
                    from litehive.runtime import _attempt_stage_recovery
                    from litehive.models import StageReport as _SR

                    append_journal(self.root, task, f"{reason}. Launching recovery agent.")
                    failed_report = _SR(
                        task_id=task.id,
                        step=current,
                        verdict="fail",
                        summary=reason,
                        feedback=f"Unhandled error: {exc}\n\nThe task was at stage '{current}' when this error occurred. "
                        f"Investigate the error, fix whatever is broken, and ensure the task can proceed.",
                        failure_classification="stage_exception",
                        failure_diagnostics={"traceback": traceback_text, "exception": str(exc)},
                    )
                    engine_name = task.engine or (
                        self.config.default_engine if self.config else "codex"
                    )
                    recovered = _attempt_stage_recovery(
                        self.root,
                        self.root,
                        task,
                        current,
                        failed_report,
                        subagents=self.subagents,
                        config=self.config,
                        engine_name=engine_name,
                    )
                    if recovered is not None:
                        report = recovered
                        report.retry_count = rejections
                        report.retry_limit = self.max_retries
                        report.retry_source = self.retry_source
                        self._write_report(task, report, steps + 1)
                        _apply_stage_finished(task, report)
                        steps += 1
                        last_verdict = report.verdict
                        if report.retry_decision == "retry":
                            task.pipeline_status = current  # type: ignore[assignment]
                            task.status = "queued"
                            return self._finish_run(
                                task,
                                final_status="queued",
                                steps=steps,
                                last_verdict=report.verdict,
                            )
                        if recovered.verdict in ("pass", "accept"):
                            continue  # proceed to next stage
                        outcome = "blocked" if report.verdict == "blocked" else "flagged"
                        outcome_reason_code = (
                            _reason_code_for_report(report)
                            if report.verdict == "blocked"
                            else "stage_exception"
                        )
                        task.status = "flagged"
                        _apply_task_outcome(
                            task,
                            kind=outcome,
                            stage=current,
                            reason_code=outcome_reason_code,
                            reason=report.summary,
                            retry_count=rejections,
                            retry_limit=self.max_retries,
                            retry_source=self.retry_source,
                            failure_classification=report.failure_classification,
                            failure_diagnostics=report.failure_diagnostics,
                        )
                        return self._finish_run(
                            task,
                            final_status=task.status,
                            steps=steps,
                            last_verdict=report.verdict,
                        )

                task.pipeline_status = current  # type: ignore[assignment]
                report = self._terminal_report(
                    task,
                    step=current,
                    verdict="fail",
                    summary=reason,
                    outcome="flagged",
                    outcome_reason_code="stage_exception",
                    reason=reason,
                    retry_count=rejections,
                    warnings=[str(exc)],
                )
                task.status = "queued"
                _apply_task_outcome(
                    task,
                    kind="flagged",
                    stage=current,
                    reason_code="stage_exception",
                    reason=reason,
                    retry_count=rejections,
                    retry_limit=self.max_retries,
                    retry_source=self.retry_source,
                )
                self._write_report(task, report, steps + 1)
                _apply_stage_finished(task, report)
                return self._finish_run(
                    task,
                    final_status="queued",
                    steps=steps + 1,
                    last_verdict="fail",
                )
            report.retry_count = rejections
            report.retry_limit = self.max_retries
            report.retry_source = self.retry_source
            if current in {"grooming", "accepting"} and report.follow_up_tasks:
                created_follow_ups = create_follow_up_tasks(
                    self.root,
                    parent_task=task,
                    stage=current,
                    follow_ups=report.follow_up_tasks,
                )
                if created_follow_ups:
                    report.created_follow_up_task_ids = [item.id for item in created_follow_ups]
                    append_journal(
                        self.root,
                        task,
                        (
                            f"Created follow-up tasks during `{current}`: "
                            f"{', '.join(item.id for item in created_follow_ups)}."
                        ),
                    )

            steps += 1
            last_verdict = report.verdict
            target = routes.get((current, report.verdict))
            if target is None and report.verdict == "reject" and current != "commit_to_git":
                report.retry_decision = "retry"
                self._write_report(task, report, steps)
                _apply_stage_finished(task, report)
                task.pipeline_status = current  # type: ignore[assignment]
                task.status = "queued"
                return self._finish_run(
                    task,
                    final_status="queued",
                    steps=steps,
                    last_verdict=last_verdict,
                )
            if target is None:
                outcome = "blocked" if report.verdict == "blocked" else "flagged"
                reason = (
                    report.summary or f"{current} returned unsupported verdict `{report.verdict}`"
                )
                outcome_reason_code = (
                    _reason_code_for_report(report)
                    if report.verdict == "blocked"
                    else "unsupported_verdict"
                )
                terminal = self._terminal_report(
                    task,
                    step=report.step,
                    verdict=report.verdict,
                    summary=report.summary,
                    outcome=outcome,
                    outcome_reason_code=outcome_reason_code,
                    reason=reason,
                    retry_count=report.retry_count,
                    warnings=report.warnings,
                    feedback=report.feedback,
                    files_changed=report.files_changed,
                    tests=report.tests,
                    resource_limit_event=report.resource_limit_event,
                    hook_results=report.hook_results,
                )
                recovered = self._launch_recovery_agent(task, current, terminal)
                if recovered is not None:
                    result = self._apply_recovery_result(
                        task, current, recovered, rejections, steps
                    )
                    if result is not None:
                        return result
                    report.warnings = [*report.warnings, *recovered.warnings]
                report = terminal
                self._write_report(task, report, steps)
                task.status = "flagged"
                _apply_task_outcome(
                    task,
                    kind=outcome,
                    stage=current,
                    reason_code=outcome_reason_code,
                    reason=reason,
                    retry_count=report.retry_count,
                    retry_limit=self.max_retries,
                    retry_source=self.retry_source,
                    failure_classification=report.failure_classification,
                    failure_diagnostics=report.failure_diagnostics,
                )
                _apply_stage_finished(task, report)
                return self._finish_run(
                    task,
                    final_status=task.status,
                    steps=steps,
                    last_verdict=last_verdict,
                )

            if current == "grooming" and target == "implementing":
                from litehive.tasks import apply_task_updates_from_report

                apply_task_updates_from_report(self.root, task, report)
                if task.status in CLOSED_TASK_STATUSES | {"parked"}:
                    return self._finish_run(
                        task,
                        final_status=task.status,
                        steps=steps,
                        last_verdict=last_verdict,
                    )
                missing_criteria_reason = missing_acceptance_criteria_reason(task)
                if missing_criteria_reason is not None:
                    report = self._terminal_report(
                        task,
                        step=current,
                        verdict="blocked",
                        summary=missing_criteria_reason,
                        outcome="blocked",
                        outcome_reason_code="missing_acceptance_criteria",
                        reason=missing_criteria_reason,
                        retry_count=report.retry_count,
                        feedback=report.feedback,
                        files_changed=report.files_changed,
                        tests=report.tests,
                        warnings=report.warnings,
                        resource_limit_event=report.resource_limit_event,
                        hook_results=report.hook_results,
                    )
                    last_verdict = report.verdict
                    task.status = "flagged"
                    _apply_task_outcome(
                        task,
                        kind="blocked",
                        stage=current,
                        reason_code="missing_acceptance_criteria",
                        reason=missing_criteria_reason,
                        retry_count=report.retry_count,
                        retry_limit=self.max_retries,
                        retry_source=self.retry_source,
                        failure_classification=report.failure_classification,
                        failure_diagnostics=report.failure_diagnostics,
                    )
                    self._write_report(task, report, steps)
                    _apply_stage_finished(task, report)
                    return self._finish_run(
                        task,
                        final_status="flagged",
                        steps=steps,
                        last_verdict=last_verdict,
                    )

            # Single mode: skip commit_to_git when no code changes were produced.
            if single_mode and current == "implementing" and target == "commit_to_git":
                if not report.files_changed:
                    target = "done"

            if target == "implementing" and current in {"testing", "accepting"}:
                rejections += 1
                report.retry_count = rejections
                report.retry_limit = self.max_retries
                stage_count = task.runtime.stage_retry_counts.get(current, 0) + 1
                task.runtime.stage_retry_counts[current] = stage_count
                effective_stage_limit = (
                    task.retry_policy.stage_retry_limit
                    if task.retry_policy.stage_retry_limit is not None
                    else self.stage_retry_limit
                )
                _apply_task_retry_state(
                    task,
                    retry_count=rejections,
                    retry_limit=self.max_retries,
                    retry_source=self.retry_source,
                )
                if stage_count > effective_stage_limit:
                    if current == "testing":
                        escalation_target = "recovery"
                        escalation_kind = "recovery escalation"
                    else:
                        escalation_target = "planner"
                        escalation_kind = "planner escalation"
                    escalation_reason = (
                        f"Stage retry limit exhausted for `{current}` "
                        f"({stage_count} rejection(s), limit: {effective_stage_limit}); "
                        f"escalating to grooming for {escalation_kind}"
                    )
                    report.outcome_reason_code = "stage_retry_limit_exhausted"
                    report.outcome_reason = escalation_reason
                    report.retry_decision = "retry"
                    self._write_report(task, report, steps)
                    _apply_stage_finished(task, report)
                    task.runtime.continuation_handoff = RuntimeContinuationHandoff(
                        step=current,
                        kind="restart",
                        reason=escalation_reason,
                        summary=(
                            f"Repeated {current} rejections ({stage_count}) "
                            f"triggered {escalation_kind} reroute to grooming. "
                            f"Retry count: {rejections}/{self.max_retries}."
                        ),
                    )
                    task.pipeline_status = "grooming"  # type: ignore[assignment]
                    task.status = "queued"
                    record_recovery_report(
                        self.root,
                        task,
                        trigger="same_stage_retry_churn",
                        stage=current,
                        summary=escalation_reason,
                        runnable_state="runnable",
                        failure_classification="stage_retry_limit_exhausted",
                        actions=[
                            RecoveryAction(
                                action="reroute_to_planner",
                                summary="Rerouted the task to grooming after same-stage retry churn.",
                                metadata={
                                    "from_stage": current,
                                    "to_stage": "grooming",
                                    "rejections": stage_count,
                                    "limit": effective_stage_limit,
                                    "owner": escalation_target,
                                },
                            )
                        ],
                    )
                    append_journal(self.root, task, escalation_reason)
                    return self._finish_run(
                        task,
                        final_status="queued",
                        steps=steps,
                        last_verdict=last_verdict,
                    )
                if rejections > self.max_retries:
                    exhausted_reason = (
                        f"Retry limit exhausted after {rejections} rejection(s) "
                        f"(limit: {self.max_retries})"
                    )
                    exhausted_report = self._terminal_report(
                        task,
                        step=current,
                        verdict=report.verdict,
                        summary=exhausted_reason,
                        outcome="flagged",
                        outcome_reason_code="retry_limit_exhausted",
                        reason=exhausted_reason,
                        retry_count=rejections,
                        feedback=report.feedback,
                        files_changed=report.files_changed,
                        tests=report.tests,
                        warnings=report.warnings,
                        resource_limit_event=report.resource_limit_event,
                        hook_results=report.hook_results,
                    )
                    recovered = self._launch_recovery_agent(task, current, exhausted_report)
                    if recovered is not None:
                        result = self._apply_recovery_result(
                            task, current, recovered, rejections, steps
                        )
                        if result is not None:
                            return result
                    report = exhausted_report
                    task.status = "flagged"
                    _apply_task_outcome(
                        task,
                        kind="flagged",
                        stage=current,
                        reason_code="retry_limit_exhausted",
                        reason=exhausted_reason,
                        retry_count=rejections,
                        retry_limit=self.max_retries,
                        retry_source=self.retry_source,
                        failure_classification=report.failure_classification,
                        failure_diagnostics=report.failure_diagnostics,
                    )
                    self._write_report(task, report, steps)
                    _apply_stage_finished(task, report)
                    return self._finish_run(
                        task,
                        final_status="flagged",
                        steps=steps,
                        last_verdict=last_verdict,
                    )
                report.retry_decision = "retry"
                self._write_report(task, report, steps)
                _apply_stage_finished(task, report)
                task.pipeline_status = target  # type: ignore[assignment]
                task.status = "queued"
                return self._finish_run(
                    task,
                    final_status="queued",
                    steps=steps,
                    last_verdict=last_verdict,
                )

            if target == "done":
                self._write_report(task, report, steps)
                _apply_stage_finished(task, report)
                if current != "commit_to_git":
                    task.pipeline_status = "done"
                    task.status = "done"
                return self._finish_run(
                    task,
                    final_status="done",
                    steps=steps,
                    last_verdict=last_verdict,
                )

            checkpoint_reason = _human_checkpoint_reason(task, target)
            if checkpoint_reason is not None:
                if report.retry_decision != "retry":
                    _clear_task_outcome(task)
                self._write_report(task, report, steps)
                _apply_stage_finished(task, report)
                task.pipeline_status = target  # type: ignore[assignment]
                task.status = "queued"
                append_journal(
                    self.root,
                    task,
                    f"Execution paused for human review at `{checkpoint_reason}`.",
                )
                return self._finish_run(
                    task,
                    final_status="paused",
                    steps=steps,
                    last_verdict=last_verdict,
                )

            if target == "flagged":
                outcome = "blocked" if report.verdict == "blocked" else "flagged"
                reason = report.summary or f"{current} ended with `{report.verdict}`"
                terminal = self._terminal_report(
                    task,
                    step=report.step,
                    verdict=report.verdict,
                    summary=report.summary,
                    outcome=outcome,
                    outcome_reason_code=_reason_code_for_report(report),
                    reason=reason,
                    retry_count=report.retry_count,
                    warnings=report.warnings,
                    feedback=report.feedback,
                    files_changed=report.files_changed,
                    tests=report.tests,
                    resource_limit_event=report.resource_limit_event,
                    hook_results=report.hook_results,
                    failure_classification=report.failure_classification,
                    failure_diagnostics=report.failure_diagnostics,
                )
                recovered = self._launch_recovery_agent(task, current, terminal)
                if recovered is not None:
                    result = self._apply_recovery_result(
                        task, current, recovered, rejections, steps
                    )
                    if result is not None:
                        return result
                    report.warnings = [*report.warnings, *recovered.warnings]
                report = terminal
                task.status = "flagged"
                _apply_task_outcome(
                    task,
                    kind=outcome,
                    stage=current,
                    reason_code=_reason_code_for_report(report),
                    reason=reason,
                    retry_count=report.retry_count,
                    retry_limit=self.max_retries,
                    retry_source=self.retry_source,
                    failure_classification=report.failure_classification,
                    failure_diagnostics=report.failure_diagnostics,
                )
                self._write_report(task, report, steps)
                _apply_stage_finished(task, report)
                return self._finish_run(
                    task,
                    final_status="flagged",
                    steps=steps,
                    last_verdict=last_verdict,
                )

            if report.retry_decision != "retry":
                clear_task_outcome(self.root, task)
            self._write_report(task, report, steps)
            mark_stage_finished(self.root, task, report)
            task.pipeline_status = target  # type: ignore[assignment]
            save_task(self.root, task)

    def _launch_recovery_agent(
        self,
        task: TaskRecord,
        step: str,
        failed_report: StageReport,
    ) -> StageReport | None:
        if self.subagents is None:
            return None
        from litehive.runtime._recovery import _attempt_stage_recovery

        engine_name = task.engine or (self.config.default_engine if self.config else "codex")
        try:
            return _attempt_stage_recovery(
                self.root,
                self.root,
                task,
                step,
                failed_report,
                subagents=self.subagents,
                config=self.config,
                engine_name=engine_name,
            )
        except Exception:
            return None

    def _apply_recovery_result(
        self,
        task: TaskRecord,
        step: str,
        recovered: StageReport,
        rejections: int,
        steps: int,
    ) -> RunResult | None:
        recovered.retry_count = rejections
        recovered.retry_limit = self.max_retries
        recovered.retry_source = self.retry_source
        self._write_report(task, recovered, steps)
        _apply_stage_finished(task, recovered)
        if recovered.verdict in ("pass", "accept"):
            next_target = _ROUTES.get((step, recovered.verdict))
            if next_target is not None:
                task.pipeline_status = next_target
                task.status = "queued"
                save_task(self.root, task)
                return self._finish_run(
                    task,
                    final_status="queued",
                    steps=steps,
                    last_verdict=recovered.verdict,
                )
        if recovered.retry_decision == "retry":
            task.pipeline_status = step
            task.status = "queued"
            save_task(self.root, task)
            return self._finish_run(
                task,
                final_status="queued",
                steps=steps,
                last_verdict=recovered.verdict,
            )
        return None

    def _finish_run(
        self,
        task: TaskRecord,
        *,
        final_status: str,
        steps: int,
        last_verdict: str | None,
    ) -> RunResult:
        finish_task_run_transition(self.root, task, final_status)
        return RunResult(final_status=final_status, steps_executed=steps, last_verdict=last_verdict)

    def _write_report(self, task: TaskRecord, report: StageReport, ordinal: int) -> None:
        if report.duration_seconds == 0:
            started_at = task.runtime.current_stage.started_at
            if started_at is not None:
                from litehive.tasks import _duration_seconds
                from litehive.models import utcnow

                report.duration_seconds = _duration_seconds(started_at, utcnow())
        reports_dir = task_dir(self.root, task) / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{report.step}-{ordinal:03d}.yaml"
        (reports_dir / filename).write_text(
            yaml.safe_dump(report.model_dump(mode="python"), sort_keys=False),
            encoding="utf-8",
        )

    def _terminal_report(
        self,
        task: TaskRecord,
        *,
        step: str,
        verdict: str,
        summary: str,
        outcome: OutcomeKind,
        outcome_reason_code: OutcomeReasonCode,
        reason: str,
        retry_count: int,
        feedback: str = "",
        files_changed: list[str] | None = None,
        tests: dict[str, int] | None = None,
        warnings: list[str] | None = None,
        resource_limit_event=None,
        hook_results: list[dict[str, str | int | bool | None]] | None = None,
        failure_classification: str | None = None,
        failure_diagnostics: dict[str, str | int | bool | None | list[str]] | None = None,
    ) -> StageReport:
        return StageReport(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            verdict=verdict,  # type: ignore[arg-type]
            summary=summary,
            feedback=feedback,
            files_changed=files_changed or [],
            tests=tests or {"added": 0, "passing": 0},
            warnings=warnings or [],
            retry_count=retry_count,
            retry_limit=self.max_retries,
            retry_source=self.retry_source,  # type: ignore[arg-type]
            retry_decision="final",
            outcome=outcome,
            outcome_reason_code=outcome_reason_code,
            outcome_reason=reason,
            failure_classification=failure_classification,
            failure_diagnostics=dict(failure_diagnostics or {}),
            resource_limit_event=resource_limit_event,
            hook_results=hook_results or [],
        )


def _reason_code_for_report(report: StageReport) -> OutcomeReasonCode:
    verdict = report.verdict
    if verdict == "fail":
        return "verdict_fail"
    if verdict == "reject":
        return "verdict_reject"
    if report.resource_limit_event is not None:
        return "resource_limit"
    return "verdict_blocked"


def _human_checkpoint_reason(task: TaskRecord, target: str) -> str | None:
    if target == "accepting" and "before_acceptance" in task.human_checkpoints:
        return "before_acceptance"
    if target == "commit_to_git" and "before_commit" in task.human_checkpoints:
        return "before_commit"
    return None
