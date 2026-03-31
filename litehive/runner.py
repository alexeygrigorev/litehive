"""Deterministic local task runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from litehive.models import OutcomeKind, OutcomeReasonCode, StageReport, TaskRecord
from litehive.tasks import (
    append_journal,
    clear_task_outcome,
    mark_stage_finished,
    mark_stage_started,
    mark_task_outcome,
    missing_acceptance_criteria_reason,
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
        rejections = 0
        last_verdict: str | None = None

        if task.pipeline_status == "done":
            return RunResult(final_status="done")

        set_task_retry_state(
            self.root,
            task,
            retry_count=0,
            retry_limit=self.max_retries,
            retry_source=self.retry_source,
        )
        clear_task_outcome(self.root, task)

        while True:
            current = _STEPS_FROM.get(task.pipeline_status)
            if current is None:
                return RunResult(final_status=task.status, steps_executed=steps, last_verdict=last_verdict)

            missing_criteria_reason = (
                missing_acceptance_criteria_reason(task) if current == "implementing" else None
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
                mark_task_outcome(
                    self.root,
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
                mark_stage_finished(self.root, task, report)
                return RunResult("flagged", steps + 1, "blocked")

            task.status = "in_progress"
            task.pipeline_status = current  # type: ignore[assignment]
            save_task(self.root, task)
            mark_stage_started(self.root, task, current)

            try:
                report = self.executor(task, current)
            except KeyboardInterrupt:
                reason = f"Execution cancelled during {current}"
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
                task.status = "cancelled"
                mark_task_outcome(
                    self.root,
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
                mark_stage_finished(self.root, task, report)
                return RunResult("cancelled", steps + 1, "blocked")
            except Exception as exc:
                reason = f"{current} failed with unhandled error: {exc}"
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
                task.status = "failed"
                mark_task_outcome(
                    self.root,
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
                mark_stage_finished(self.root, task, report)
                return RunResult("failed", steps + 1, "fail")
            report.retry_count = rejections
            report.retry_limit = self.max_retries
            report.retry_source = self.retry_source

            steps += 1
            last_verdict = report.verdict
            target = _ROUTES.get((current, report.verdict))
            if target is None:
                outcome = "blocked" if report.verdict == "blocked" else "failed"
                reason = report.summary or f"{current} returned unsupported verdict `{report.verdict}`"
                outcome_reason_code = (
                    _reason_code_for_verdict(report.verdict)
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
                )
                self._write_report(task, report, steps)
                task.status = "flagged" if outcome == "blocked" else "failed"
                mark_task_outcome(
                    self.root,
                    task,
                    kind=outcome,
                    stage=current,
                    reason_code=outcome_reason_code,
                    reason=reason,
                    retry_count=report.retry_count,
                    retry_limit=self.max_retries,
                    retry_source=self.retry_source,
                )
                mark_stage_finished(self.root, task, report)
                return RunResult(task.status, steps, last_verdict)

            if current == "grooming" and target == "implementing":
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
                    )
                    last_verdict = report.verdict
                    task.status = "flagged"
                    mark_task_outcome(
                        self.root,
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
                    mark_stage_finished(self.root, task, report)
                    return RunResult("flagged", steps, last_verdict)

            if target == "implementing" and current in {"testing", "accepting"}:
                rejections += 1
                report.retry_count = rejections
                report.retry_limit = self.max_retries
                set_task_retry_state(
                    self.root,
                    task,
                    retry_count=rejections,
                    retry_limit=self.max_retries,
                    retry_source=self.retry_source,
                )
                if rejections > self.max_retries:
                    reason = (
                        f"Retry limit ({self.max_retries}) exceeded after {rejections} rejection(s)"
                        f" during {current}"
                    )
                    report.retry_decision = "flagged"
                    report = self._terminal_report(
                        task,
                        step=report.step,
                        verdict=report.verdict,
                        summary=report.summary,
                        outcome="flagged",
                        outcome_reason_code="retry_limit_exhausted",
                        reason=reason,
                        retry_count=rejections,
                        warnings=report.warnings,
                        feedback=report.feedback,
                        files_changed=report.files_changed,
                        tests=report.tests,
                    )
                    task.status = "flagged"
                    mark_task_outcome(
                        self.root,
                        task,
                        kind="flagged",
                        stage=current,
                        reason_code="retry_limit_exhausted",
                        reason=reason,
                        retry_count=rejections,
                        retry_limit=self.max_retries,
                        retry_source=self.retry_source,
                    )
                    self._write_report(task, report, steps)
                    mark_stage_finished(self.root, task, report)
                    return RunResult("flagged", steps, last_verdict)
                report.retry_decision = "retry"

            if target == "done":
                self._write_report(task, report, steps)
                mark_stage_finished(self.root, task, report)
                if current != "commit_to_git":
                    task.pipeline_status = "done"
                    task.status = "done"
                return RunResult("done", steps, last_verdict)

            checkpoint_reason = _human_checkpoint_reason(task, target)
            if checkpoint_reason is not None:
                if report.retry_decision != "retry":
                    clear_task_outcome(self.root, task)
                self._write_report(task, report, steps)
                mark_stage_finished(self.root, task, report)
                task.pipeline_status = target  # type: ignore[assignment]
                task.status = "queued"
                append_journal(
                    self.root,
                    task,
                    f"Execution paused for human review at `{checkpoint_reason}`.",
                )
                save_task(self.root, task)
                return RunResult("paused", steps, last_verdict)

            if target == "flagged":
                outcome = "blocked" if report.verdict == "blocked" else "flagged"
                reason = report.summary or f"{current} ended with `{report.verdict}`"
                report = self._terminal_report(
                    task,
                    step=report.step,
                    verdict=report.verdict,
                    summary=report.summary,
                    outcome=outcome,
                    outcome_reason_code=_reason_code_for_verdict(report.verdict),
                    reason=reason,
                    retry_count=report.retry_count,
                    warnings=report.warnings,
                    feedback=report.feedback,
                    files_changed=report.files_changed,
                    tests=report.tests,
                )
                task.status = "flagged"
                mark_task_outcome(
                    self.root,
                    task,
                    kind=outcome,
                    stage=current,
                    reason_code=_reason_code_for_verdict(report.verdict),
                    reason=reason,
                    retry_count=report.retry_count,
                    retry_limit=self.max_retries,
                    retry_source=self.retry_source,
                )
                self._write_report(task, report, steps)
                mark_stage_finished(self.root, task, report)
                return RunResult("flagged", steps, last_verdict)

            if report.retry_decision != "retry":
                clear_task_outcome(self.root, task)
            self._write_report(task, report, steps)
            mark_stage_finished(self.root, task, report)
            task.pipeline_status = target  # type: ignore[assignment]
            save_task(self.root, task)

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
        )


def _reason_code_for_verdict(verdict: str) -> OutcomeReasonCode:
    if verdict == "fail":
        return "verdict_fail"
    if verdict == "reject":
        return "verdict_reject"
    return "verdict_blocked"


def _human_checkpoint_reason(task: TaskRecord, target: str) -> str | None:
    if target == "accepting" and "before_acceptance" in task.human_checkpoints:
        return "before_acceptance"
    if target == "commit_to_git" and "before_commit" in task.human_checkpoints:
        return "before_commit"
    return None
