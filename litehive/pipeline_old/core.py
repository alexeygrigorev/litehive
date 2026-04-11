"""Core task execution runner."""

from dataclasses import dataclass
from pathlib import Path
import subprocess
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
from litehive.tasks.constants import CLOSED_TASK_STATUSES
from litehive.tasks.crud import save_task
from litehive.tasks.journal import append_journal
from litehive.tasks.normalization import missing_acceptance_criteria_reason, needs_normalization
from litehive.tasks.paths import task_dir
from litehive.tasks.reports import record_recovery_report
from litehive.pipeline_old.recovery import _prepare_interrupted_task, interruption_journal_message
from litehive.workspace.runtime_tracking import (
    _apply_stage_finished,
    _apply_task_outcome,
    _apply_task_retry_state,
    _clear_task_outcome,
    clear_task_outcome,
    finish_task_run_transition,
    mark_stage_finished,
    mark_stage_started,
    set_task_retry_state,
)
from litehive.pipeline_old.states import _ROUTES, _SINGLE_ROUTES, _SINGLE_STEPS_FROM, _STEPS_FROM


class StageExecutor(Protocol):
    def __call__(self, task: TaskRecord, step: str) -> StageReport: ...


@dataclass(slots=True)
class RunResult:
    final_status: str
    steps_executed: int = 0
    last_verdict: str | None = None


class TaskExecutionRunner:
    """Drive one task through the fixed pipeline using a deterministic router.

    The runner uses the transition table in ``litehive.pipeline_old.states._ROUTES`` to
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

    @staticmethod
    def _report_verified_preimplemented_pass(report: StageReport) -> bool:
        if not report.submitted_via_cli:
            return False
        evidence = " ".join(part for part in (report.summary, report.feedback) if part).lower()
        already_done_markers = (
            "already implemented",
            "already complete",
            "already completed",
            "already done",
            "pre-implemented",
            "preimplemented",
            "pre-existing",
            "existing implementation",
        )
        verification_markers = (
            "verified",
            "verification",
            "confirmed",
            "validated",
            "checked",
            "test",
            "tests",
            "pytest",
            "acceptance criteria",
        )
        return any(marker in evidence for marker in already_done_markers) and any(
            marker in evidence for marker in verification_markers
        )

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
                    from litehive.pipeline_old import _attempt_stage_recovery
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
                    engine_name = self.config.default_engine if self.config else "codex"
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
            steps += 1
            last_verdict = report.verdict
            target = routes.get((current, report.verdict))
            if current == "commit_to_git" and report.retry_decision == "retry":
                self._write_report(task, report, steps)
                _apply_stage_finished(task, report)
                save_task(self.root, task)
                return self._finish_run(
                    task,
                    final_status="queued",
                    steps=steps,
                    last_verdict=last_verdict,
                )
            if target is None and report.verdict == "reject" and current != "commit_to_git":
                report.retry_decision = "retry"
                if report.source == "hook":
                    append_journal(
                        self.root,
                        task,
                        f"Hook rejection routed `{current}` back to SWE for another `{current}` pass.",
                    )
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
                if current == "commit_to_git":
                    task.status = "recovery_failed"
                    append_journal(
                        self.root, task,
                        "commit_to_git failed and recovery did not resolve it. "
                        "Status set to recovery_failed for manual resolution.",
                    )
                else:
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

            # Guard: SWE must produce actual changes. If implementing "passes"
            # with zero files changed and zero tests, reject it back to implementing.
            # Check via `git status` in the worktree — simple and reliable.
            if current == "implementing" and target == "testing":
                worktree_has_changes = False
                from litehive.tasks.crud import get_task_worktree_path
                from litehive.tasks.paths import task_dir
                from litehive.git import current_head, is_git_repo
                # Skip guard if the guard already rejected this task before.
                # The guard overwrites pass→reject before saving, so we can
                # never find verdict=pass in prior reports.  Instead, detect
                # prior guard rejections by their distinctive summary prefix.
                # A second guard rejection means the task legitimately produces
                # no code (analysis/planning task) and should advance.
                _prior_reports = sorted(
                    (task_dir(self.root, task) / "reports").glob("implementing-*.yaml")
                ) if (task_dir(self.root, task) / "reports").exists() else []
                _has_prior_pass = False
                _GUARD_MARKER = "SWE reported pass but produced no file changes"
                for _rp in _prior_reports:
                    try:
                        import yaml as _yaml
                        _rd = _yaml.safe_load(_rp.read_text(encoding="utf-8")) or {}
                        if _rd.get("verdict") == "pass":
                            _has_prior_pass = True
                            break
                        if (_rd.get("verdict") == "reject"
                                and _GUARD_MARKER in (_rd.get("summary") or "")):
                            _has_prior_pass = True
                            break
                    except Exception:
                        pass
                wt_path = get_task_worktree_path(task)
                check_dir = None
                if wt_path:
                    wt_full = (self.root / wt_path).resolve()
                    if wt_full.exists():
                        check_dir = wt_full
                if check_dir is None and is_git_repo(self.root):
                    check_dir = self.root
                litehive_task_changes = False
                if check_dir is not None:
                    try:
                        # `git status --porcelain` shows all dirty state in one call:
                        # modified, staged, untracked — everything.
                        status_result = subprocess.run(
                            ["git", "status", "--porcelain"],
                            cwd=check_dir, capture_output=True, text=True, timeout=10,
                        )
                        status_files = [
                            line[3:].strip()
                            for line in status_result.stdout.splitlines()
                            if line.strip() and not line[3:].strip().startswith(".litehive/")
                        ]
                        # Track .litehive/tasks/ changes separately — planning
                        # tasks produce task records, not code.
                        litehive_task_changes = any(
                            line[3:].strip().startswith(".litehive/tasks/")
                            for line in status_result.stdout.splitlines()
                            if line.strip()
                        )
                        # Also check commits ahead of the fork point (agent may have
                        # committed its work already).
                        if wt_path:
                            main_head = current_head(self.root) or "HEAD"
                            merge_base_result = subprocess.run(
                                ["git", "merge-base", main_head, "HEAD"],
                                cwd=check_dir, capture_output=True, text=True, timeout=10,
                            )
                            fork_point = merge_base_result.stdout.strip() if merge_base_result.returncode == 0 else None
                            if fork_point:
                                committed = subprocess.run(
                                    ["git", "diff", "--name-only", fork_point, "HEAD"],
                                    cwd=check_dir, capture_output=True, text=True, timeout=10,
                                )
                                for f in committed.stdout.splitlines():
                                    f = f.strip()
                                    if not f:
                                        continue
                                    if f.startswith(".litehive/tasks/"):
                                        litehive_task_changes = True
                                    elif not f.startswith(".litehive/"):
                                        status_files.append(f)
                        all_changed = set(f for f in status_files if f)
                        if all_changed:
                            worktree_has_changes = True
                            report.files_changed = sorted(all_changed)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
                if check_dir is None:
                    pass
                elif _has_prior_pass:
                    # Task already passed implementing in a prior run — skip the
                    # guard to avoid infinite flag/recover loops for analysis or
                    # planning tasks that re-enter implementing via recovery.
                    pass
                elif self._report_verified_preimplemented_pass(report):
                    # A zero-change implementing pass is valid when the agent
                    # explicitly submitted a CLI verdict explaining that the
                    # requested work was already present and verified.
                    pass
                elif litehive_task_changes:
                    # Planning/analysis task: produced task records under
                    # .litehive/tasks/ but no code changes — this is valid output.
                    pass
                elif not worktree_has_changes and report.files_changed:
                    claimed_files = sorted({path for path in report.files_changed if path})
                    reason = (
                        "agent reported changes but worktree is clean — likely hallucination. "
                        "The implementing pass claimed file changes, but `git status --porcelain` "
                        "found no tracked or untracked code changes in the task worktree, so the "
                        "completion report was rejected and its claimed files were not trusted. "
                        f"Claimed files: {', '.join(claimed_files)}. "
                        f"Report summary: {report.summary}"
                    )
                    append_journal(
                        self.root,
                        task,
                        "[guard] Rejected hallucinated SWE completion: "
                        "prior completion claim was bogus because the worktree was clean. "
                        f"Claimed files: {', '.join(claimed_files)}.",
                    )
                    report.verdict = "reject"
                    report.summary = reason
                    report.outcome_reason_code = "hallucinated_completion"
                    report.outcome_reason = (
                        "Rejected implementing pass because the agent claimed file changes "
                        "but the task worktree was clean."
                    )
                    report.failure_classification = "hallucinated_completion"
                    report.failure_diagnostics = {
                        "claimed_files": claimed_files,
                        "worktree_has_changes": False,
                    }
                    report.files_changed = []
                    target = "implementing"
                elif not worktree_has_changes and (not report.tests or report.tests.get("added", 0) == 0):
                    reason = (
                        "SWE reported pass but produced no file changes and no tests. "
                        "This usually means the agent did not actually write code. "
                        f"Worktree: {getattr(self, '_execution_root', 'unknown')}. "
                        f"Report summary: {report.summary}"
                    )
                    append_journal(self.root, task, f"[guard] Rejected empty SWE pass: {reason}")
                    report.verdict = "reject"
                    report.summary = reason
                    target = "implementing"

            if current == "grooming" and target == "implementing":
                from litehive.workspace.workflow import apply_task_updates_from_report

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

            if target == "implementing" and current in {"implementing", "testing", "accepting"}:
                rejections += 1
                report.retry_count = rejections
                report.retry_limit = self.max_retries
                if report.source == "hook":
                    append_journal(
                        self.root,
                        task,
                        f"Hook rejection routed `{current}` back to `implementing` for SWE follow-up.",
                    )
                # Hook rejections go straight back to SWE — no stage escalation
                if report.source != "hook" and current in {"testing", "accepting"}:
                    stage_count = task.runtime.stage_retry_counts.get(current, 0) + 1
                    task.runtime.stage_retry_counts[current] = stage_count
                else:
                    stage_count = 0
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
                    rejection_feedback = (report.feedback or "").strip()
                    summary_parts = [
                        f"Repeated {current} rejections ({stage_count}) "
                        f"triggered {escalation_kind} reroute to grooming. "
                        f"Retry count: {rejections}/{self.max_retries}.",
                    ]
                    if rejection_feedback:
                        summary_parts.append(
                            f"\n\nLast rejection output:\n{rejection_feedback}"
                        )
                    task.runtime.continuation_handoff = RuntimeContinuationHandoff(
                        step=current,
                        kind="restart",
                        reason=escalation_reason,
                        summary="\n".join(summary_parts),
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
                rejection_feedback = (report.feedback or "").strip()
                retry_summary = (
                    f"{current} rejected (attempt {rejections}/{self.max_retries}). "
                    f"Routing back to implementing for fixes."
                )
                if rejection_feedback:
                    retry_summary += f"\n\nRejection output:\n{rejection_feedback}"
                task.runtime.continuation_handoff = RuntimeContinuationHandoff(
                    step=target,
                    kind="retry",
                    reason=report.summary or f"{current} rejected",
                    summary=retry_summary,
                )
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

            if target == "merge_failed":
                reason = report.summary or f"{current} merge failed"
                terminal = self._terminal_report(
                    task,
                    step=report.step,
                    verdict=report.verdict,
                    summary=report.summary,
                    outcome="flagged",
                    outcome_reason_code="merge_conflict",
                    reason=reason,
                    retry_count=report.retry_count,
                    warnings=report.warnings,
                    feedback=report.feedback,
                    files_changed=report.files_changed,
                    tests=report.tests,
                    resource_limit_event=report.resource_limit_event,
                    hook_results=report.hook_results,
                    failure_classification=report.failure_classification or "merge_conflict",
                    failure_diagnostics=report.failure_diagnostics,
                )
                report = terminal
                task.status = "merge_failed"
                task.pipeline_status = "merge_failed"
                _apply_task_outcome(
                    task,
                    kind="flagged",
                    stage=current,
                    reason_code="merge_conflict",
                    reason=reason,
                    retry_count=report.retry_count,
                    retry_limit=self.max_retries,
                    retry_source=self.retry_source,
                    failure_classification=report.failure_classification,
                    failure_diagnostics=report.failure_diagnostics,
                )
                _record_unmerged_worktree(self.root, task)
                self._write_report(task, report, steps)
                _apply_stage_finished(task, report)
                return self._finish_run(
                    task,
                    final_status="merge_failed",
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
                # commit_to_git: if both merge agent and recovery failed,
                # use recovery_failed terminal state to prevent infinite requeue loops.
                if current == "commit_to_git" and recovered is None:
                    final_status = "recovery_failed"
                    append_journal(
                        self.root, task,
                        "Both merge agent and recovery agent failed for commit_to_git. "
                        "Setting status to recovery_failed — manual resolution required.",
                    )
                else:
                    final_status = "flagged"
                task.status = final_status
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
                    final_status=final_status,
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
        from litehive.pipeline_old import _attempt_stage_recovery

        engine_name = self.config.default_engine if self.config else "codex"
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
                from litehive.workspace.runtime_tracking import _duration_seconds
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


def _record_unmerged_worktree(root: Path, task: TaskRecord) -> None:
    """Record a task's worktree as unmerged in state.yaml for later resolution."""
    from litehive.models import UnmergedWorktree
    from litehive.tasks.crud import get_task_worktree_path
    from litehive.tasks.persistence import load_state, save_state

    worktree_path = get_task_worktree_path(task)
    if not worktree_path:
        return
    state = load_state(root)
    # Avoid duplicates
    for entry in state.unmerged_worktrees:
        if entry.task_id == task.id:
            return
    state.unmerged_worktrees.append(
        UnmergedWorktree(task_id=task.id, worktree_path=worktree_path)
    )
    save_state(root, state)
