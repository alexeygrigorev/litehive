"""Deterministic local task runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from litehive.models import OutcomeKind, OutcomeReasonCode, StageReport, TaskRecord
from litehive.tasks import (
    _apply_task_retry_state,
    _apply_stage_finished,
    _apply_task_outcome,
    _clear_task_outcome,
    append_journal,
    clear_task_outcome,
    create_follow_up_tasks,
    finish_task_run_transition,
    mark_stage_finished,
    mark_stage_started,
    missing_acceptance_criteria_reason,
    populate_missing_acceptance_criteria_from_report,
    save_task,
    set_task_retry_state,
    task_dir,
)


class StageExecutor(Protocol):
    def __call__(self, task: TaskRecord, step: str) -> StageReport: ...


@dataclass(slots=True)
class RunResult:
    final_status: str
    steps_executed: int = 0
    last_verdict: str | None = None


_STEPS_FROM: dict[str, str] = {
    "backlog": "grooming",
    "grooming": "grooming",
    "implementing": "implementing",
    "testing": "testing",
    "accepting": "accepting",
    "commit_to_git": "commit_to_git",
}

_ROUTES: dict[tuple[str, str], str] = {
    ("grooming", "pass"): "implementing",
    ("grooming", "accept"): "implementing",
    ("implementing", "pass"): "testing",
    ("implementing", "accept"): "testing",
    ("testing", "pass"): "accepting",
    ("testing", "accept"): "accepting",
    ("testing", "fail"): "implementing",
    ("testing", "reject"): "implementing",
    ("accepting", "pass"): "commit_to_git",
    ("accepting", "accept"): "commit_to_git",
    ("accepting", "fail"): "implementing",
    ("accepting", "reject"): "implementing",
    ("commit_to_git", "pass"): "done",
    ("commit_to_git", "accept"): "done",
    ("commit_to_git", "fail"): "flagged",
    ("commit_to_git", "reject"): "flagged",
    ("commit_to_git", "blocked"): "flagged",
}


class TaskExecutionRunner:
    """Drive one task through the fixed pipeline using a deterministic router."""

    def __init__(
        self,
        root: Path,
        executor: StageExecutor,
        max_retries: int = 3,
        retry_source: str = "global",
    ) -> None:
        self.root = root
        self.executor = executor
        self.max_retries = max_retries
        self.retry_source = retry_source

    def run(self, task: TaskRecord) -> RunResult:
        steps = 0
        rejections = task.runtime.retry_count
        last_verdict: str | None = None

        if task.pipeline_status == "done":
            return RunResult(final_status="done")

        set_task_retry_state(
            self.root,
            task,
            retry_count=rejections,
            retry_limit=self.max_retries,
            retry_source=self.retry_source,
        )
        clear_task_outcome(self.root, task)

        while True:
            current = _STEPS_FROM.get(task.pipeline_status)
            if current is None:
                return self._finish_run(
                    task,
                    final_status=task.status,
                    steps=steps,
                    last_verdict=last_verdict,
                )

            missing_criteria_reason = (
                missing_acceptance_criteria_reason(task)
                if current in {"implementing", "testing", "accepting", "commit_to_git"}
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
                reason = f"Execution cancelled during {current}"
                task.pipeline_status = current  # type: ignore[assignment]
                report = self._terminal_report(
                    task,
                    step=current,
                    verdict="blocked",
                    summary=reason,
                    outcome="cancelled",
                    outcome_reason_code="execution_cancelled",
                    reason=reason,
                    retry_count=rejections,
                )
                task.status = "queued"
                _apply_task_outcome(
                    task,
                    kind="cancelled",
                    stage=current,
                    reason_code="execution_cancelled",
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
                    last_verdict="blocked",
                )
            except Exception as exc:
                reason = f"{current} failed with unhandled error: {exc}"
                task.pipeline_status = current  # type: ignore[assignment]
                report = self._terminal_report(
                    task,
                    step=current,
                    verdict="fail",
                    summary=reason,
                    outcome="failed",
                    outcome_reason_code="stage_exception",
                    reason=reason,
                    retry_count=rejections,
                    warnings=[str(exc)],
                )
                task.status = "queued"
                _apply_task_outcome(
                    task,
                    kind="failed",
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
            target = _ROUTES.get((current, report.verdict))
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
                outcome = "blocked" if report.verdict == "blocked" else "failed"
                reason = report.summary or f"{current} returned unsupported verdict `{report.verdict}`"
                outcome_reason_code = (
                    _reason_code_for_report(report)
                    if report.verdict == "blocked"
                    else "unsupported_verdict"
                )
                report = self._terminal_report(
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
                )
                self._write_report(task, report, steps)
                task.status = "flagged" if outcome == "blocked" else "failed"
                _apply_task_outcome(
                    task,
                    kind=outcome,
                    stage=current,
                    reason_code=outcome_reason_code,
                    reason=reason,
                    retry_count=report.retry_count,
                    retry_limit=self.max_retries,
                    retry_source=self.retry_source,
                )
                _apply_stage_finished(task, report)
                return self._finish_run(
                    task,
                    final_status=task.status,
                    steps=steps,
                    last_verdict=last_verdict,
                )

            if current == "grooming" and target == "implementing":
                populate_missing_acceptance_criteria_from_report(self.root, task, report.feedback)
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
                    )
                    self._write_report(task, report, steps)
                    _apply_stage_finished(task, report)
                    return self._finish_run(
                        task,
                        final_status="flagged",
                        steps=steps,
                        last_verdict=last_verdict,
                    )

            if target == "implementing" and current in {"testing", "accepting"}:
                rejections += 1
                report.retry_count = rejections
                report.retry_limit = self.max_retries
                _apply_task_retry_state(
                    task,
                    retry_count=rejections,
                    retry_limit=self.max_retries,
                    retry_source=self.retry_source,
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
                report = self._terminal_report(
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
                )
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
        reports_dir = task_dir(self.root, task) / "reports"
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
            resource_limit_event=resource_limit_event,
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
