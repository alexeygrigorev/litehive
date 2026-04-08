"""Stage executor builder (build_executor + _record_codex_monitoring)."""

import time
from pathlib import Path

from litehive.config import LitehiveConfig
from litehive.engines import extract_engine_continuation, get_engine
from litehive.engines._codex_quota import check_codex_quota, codex_quota_block_reason
from litehive.models import StageReport, TaskRecord, cap_feedback
from litehive.runner import StageExecutor
from litehive.subagents import SubagentManager, stage_prompt, stage_report_from_subagent
from litehive.tasks import (
    append_journal,
    mark_engine_switch,
    task_dir,
)

from ._budget import _engine_attempt_order
from ._commit import _commit_to_git_report
from ._hooks import _attach_runner_hook_results, _run_runner_hooks_for_stage
from ._models import (
    _resolve_stage_retry_limit,
    _retry_backoff_seconds,
    _role_for_step,
    _set_continuation_handoff,
    resolve_execution_retry_policy,
    resolve_model,
)
from ._recovery import _attempt_stage_recovery
from ._types import EngineBudgetLedger


def _record_codex_monitoring(root: Path, status: object) -> None:
    try:
        from litehive.observability._engine_monitoring import record_codex_quota_check

        record_codex_quota_check(root, status=status)
    except Exception:  # noqa: BLE001
        pass  # best-effort monitoring


def build_executor(
    root: Path,
    *,
    execution_root: Path,
    initial_engine_names: list[str],
    workspace_context: str,
    subagents: SubagentManager,
    config: LitehiveConfig,
    task: TaskRecord,
    model_override: str | None,
    config_auto_commit: bool,
    budget_ledger: EngineBudgetLedger,
) -> StageExecutor:
    next_stage_engine_names = list(initial_engine_names)

    def executor(current_task: TaskRecord, step: str) -> StageReport:
        nonlocal next_stage_engine_names
        if step == "commit_to_git":
            return _commit_to_git_report(
                root,
                execution_root,
                current_task,
                auto_commit_enabled=config_auto_commit and current_task.git.auto_commit,
                subagents=subagents,
                config=config,
            )

        pre_stage_hook_results: list[dict[str, str | int | bool | None]] = []
        pre_stage_hook_report = _run_runner_hooks_for_stage(
            root,
            execution_root,
            current_task,
            step=step,
            config=config,
            phase="before",
            collected_results=pre_stage_hook_results,
        )
        if pre_stage_hook_report is not None:
            return pre_stage_hook_report

        execution_events: list[str] = []
        engines = _engine_attempt_order(next_stage_engine_names, config.engine_preference)
        limit_trigger_reason: str | None = None

        for index, engine_name in enumerate(engines):
            if engine_name == "codex":
                quota_status = check_codex_quota()
                _record_codex_monitoring(root, quota_status)
                quota_reason = codex_quota_block_reason()
                if quota_reason is not None:
                    if index + 1 < len(engines):
                        next_engine = engines[index + 1]
                        event = (
                            f"Stage `{step}` switched from `{engine_name}` to `{next_engine}` "
                            f"after {quota_reason}."
                        )
                        execution_events.append(event)
                        append_journal(root, current_task, event)
                        mark_engine_switch(
                            root,
                            current_task,
                            step=step,
                            from_engine=engine_name,
                            to_engine=next_engine,
                            reason=quota_reason,
                        )
                        continue
                    report = StageReport(
                        task_id=current_task.id,
                        step=step,  # type: ignore[arg-type]
                        verdict="blocked",
                        summary=f"{step} blocked: {quota_reason}",
                        feedback="\n\n".join(execution_events).strip(),
                        warnings=[*execution_events, quota_reason],
                    )
                    return report

            budget_reason = budget_ledger.block_reason(engine_name)
            if budget_reason is not None:
                if index + 1 < len(engines):
                    next_engine = engines[index + 1]
                    event = (
                        f"Stage `{step}` switched from `{engine_name}` to `{next_engine}` "
                        f"after {budget_reason}."
                    )
                    execution_events.append(event)
                    append_journal(root, current_task, event)
                    mark_engine_switch(
                        root,
                        current_task,
                        step=step,
                        from_engine=engine_name,
                        to_engine=next_engine,
                        reason=budget_reason,
                    )
                    continue
                report = StageReport(
                    task_id=current_task.id,
                    step=step,  # type: ignore[arg-type]
                    verdict="blocked",
                    summary=f"{step} blocked: {budget_reason}",
                    feedback="\n\n".join(execution_events).strip(),
                    warnings=[*execution_events, budget_reason],
                )
                return report

            budget_ledger.record(engine_name)
            model_name = resolve_model(
                task, config, engine_name=engine_name, model_override=model_override
            )
            retry_policy = resolve_execution_retry_policy(
                config,
                engine_name=engine_name,
                model_name=model_name,
            )
            max_turns = config.claude_max_turns if engine_name == "claude" else None
            attempt_count = 0
            retry_exhausted_reason: str | None = None
            resume_session_id: str | None = None
            crash_resume_attempted = False
            while True:
                attempt_count += 1
                role_name = _role_for_step(step, current_task)
                if resume_session_id:
                    prompt = "Please continue where you left off. Complete the task."
                else:
                    prompt = stage_prompt(
                        current_task,
                        step,
                        workspace_context=workspace_context,
                        process_profile=config.process_profile,
                        role_name=role_name,
                        config=config,
                        root=root,
                    )
                result = subagents.run(
                    current_task,
                    role=role_name,
                    engine_name=engine_name,
                    prompt=prompt,
                    model=model_name,
                    max_turns=max_turns,
                    resume_session_id=resume_session_id,
                )
                resume_session_id = None
                failure = result.failure
                if failure is not None and failure.kind == "execution_interrupted":
                    raise KeyboardInterrupt(failure.reason)
                if failure is None and result.exit_code != 0 and not crash_resume_attempted:
                    continuation = extract_engine_continuation(engine_name, result.execution)
                    if (
                        engine_name == "claude"
                        and continuation is not None
                        and continuation.session_id
                    ):
                        crash_resume_attempted = True
                        resume_session_id = continuation.session_id
                        resume_event = (
                            f"Stage `{step}` agent crashed (exit {result.exit_code}) with no "
                            f"failure classification — resuming claude session "
                            f"{continuation.session_id}."
                        )
                        execution_events.append(resume_event)
                        append_journal(root, current_task, resume_event)
                        continue
                if (
                    failure is None
                    or failure.kind != "retryable_execution_error"
                    or failure.classification not in retry_policy.policy.retry_on
                ):
                    break
                retry_number = attempt_count - 1
                if retry_number >= retry_policy.policy.max_retries:
                    stop_event = (
                        f"Stage `{step}` stopped retrying `{engine_name}` after attempt "
                        f"{attempt_count}/{retry_policy.policy.max_retries + 1}: {failure.reason}."
                    )
                    execution_events.append(stop_event)
                    append_journal(root, current_task, stop_event)
                    retry_exhausted_reason = failure.reason
                    break
                backoff_seconds = _retry_backoff_seconds(retry_policy.policy, retry_number + 1)
                retry_event = (
                    f"Stage `{step}` retrying `{engine_name}` after attempt "
                    f"{attempt_count}/{retry_policy.policy.max_retries + 1} due to {failure.reason} "
                    f"(classification: {failure.classification}, policy: {retry_policy.selector}, "
                    f"backoff: {backoff_seconds:.2f}s)."
                )
                execution_events.append(retry_event)
                append_journal(root, current_task, retry_event)
                handoff = _set_continuation_handoff(
                    root,
                    current_task,
                    step=step,
                    kind="retry",
                    reason=failure.reason,
                    result=result,
                    from_engine=engine_name,
                    to_engine=engine_name,
                    from_model=model_name,
                    to_model=model_name,
                    attempt=attempt_count,
                )
                if (
                    engine_name == "claude"
                    and handoff.continuation
                    and handoff.continuation.session_id
                ):
                    resume_session_id = handoff.continuation.session_id
                if backoff_seconds > 0:
                    time.sleep(backoff_seconds)
            is_limit_failure = (
                result.failure is not None and result.failure.kind == "execution_limit"
            )
            is_retry_exhausted_failure = retry_exhausted_reason is not None
            if is_limit_failure and limit_trigger_reason is None:
                limit_trigger_reason = result.failure.reason
            is_unavailable_fallback = (
                limit_trigger_reason is not None
                and result.failure is not None
                and result.failure.kind == "engine_error"
            )
            if (
                is_limit_failure or is_unavailable_fallback or is_retry_exhausted_failure
            ) and index + 1 < len(engines):
                next_engine = engines[index + 1]
                failure_reason = (
                    result.failure.reason if result.failure is not None else retry_exhausted_reason
                )
                event = (
                    f"Stage `{step}` switched from `{engine_name}` to `{next_engine}` "
                    f"after {failure_reason}."
                )
                execution_events.append(event)
                append_journal(root, current_task, event)
                _set_continuation_handoff(
                    root,
                    current_task,
                    step=step,
                    kind="engine_switch",
                    reason=failure_reason,
                    result=result,
                    from_engine=engine_name,
                    to_engine=next_engine,
                    from_model=model_name,
                    to_model=resolve_model(
                        task,
                        config,
                        engine_name=next_engine,
                        model_override=model_override,
                    ),
                    attempt=attempt_count,
                )
                mark_engine_switch(
                    root,
                    current_task,
                    step=step,
                    from_engine=engine_name,
                    to_engine=next_engine,
                    reason=failure_reason,
                )
                continue

            # If the agent didn't submit a verdict via `litehive report` and we
            # have a session to resume, ask it to submit one.
            from litehive.tasks import load_task_thread

            thread_comments = [
                c
                for c in load_task_thread(root, current_task)
                if c.step == step and c.verdict != "comment"
            ]
            if (
                not thread_comments
                and result.failure is None
                and result.execution is not None
            ):
                continuation = extract_engine_continuation(engine_name, result.execution)
                if continuation and continuation.session_id:
                    nudge_prompt = (
                        f"You finished the {step} stage but did not submit your verdict. "
                        f"Please run this command now:\n\n"
                        f"  litehive report --task-id {current_task.id} --verdict <pass|fail|reject> --role {role_name} "
                        f'--step {step} --message "<your detailed report>"\n\n'
                        f"IMPORTANT: Always use --task-id {current_task.id} to ensure the report goes to the right task.\n\n"
                        f"Your report is the ONLY thing the next agent will read. Include:\n"
                        f"- What you did and what the outcome was\n"
                        f"- If rejecting: exact failures, which files to fix, step-by-step instructions\n"
                        f"- If passing: what evidence confirms the acceptance criteria are met\n"
                        f"Do NOT write a vague summary. Be specific and actionable."
                    )
                    subagents.run(
                        current_task,
                        role=role_name,
                        engine_name=engine_name,
                        prompt=nudge_prompt,
                        model=model_name,
                        resume_session_id=continuation.session_id,
                    )

            report = stage_report_from_subagent(current_task, step, result, root=root)

            # Launch recovery agent in two cases:
            # 1. Something broke (engine error/crash on a stage)
            # 2. Too many rejections on the same step (stuck in a loop)
            from litehive.tasks import task_dir as _task_dir

            _reports_dir = _task_dir(root, current_task) / "reports"
            prior_attempts = (
                len(list(_reports_dir.glob(f"{step}-*.yaml"))) if _reports_dir.exists() else 0
            )
            is_engine_break = (
                result.failure is not None
                and result.failure.kind in ("retryable_execution_error", "engine_error")
                and step != "commit_to_git"
            )
            is_stuck_loop = (
                report.verdict in ("fail", "reject")
                and step in ("implementing", "testing")
                and prior_attempts >= 3
            )
            is_blocked_stage = report.verdict == "blocked" and step != "commit_to_git"
            if is_engine_break or is_stuck_loop or is_blocked_stage:
                recovered_report = _attempt_stage_recovery(
                    root,
                    execution_root,
                    current_task,
                    step,
                    report,
                    subagents=subagents,
                    config=config,
                    role_name=role_name,
                    engine_name=engine_name,
                    model_name=model_name,
                )
                if recovered_report is not None:
                    report = recovered_report

            if execution_events:
                report.warnings = [*execution_events, *report.warnings]
                report.feedback = cap_feedback(
                    "\n\n".join([*execution_events, report.feedback]).strip()
                )
            _attach_runner_hook_results(report, pre_stage_hook_results)
            if limit_trigger_reason is not None and (is_limit_failure or is_unavailable_fallback):
                # Execution-limit fallback exhaustion is an infrastructure
                # issue, not a task-quality issue — verdict must be "blocked"
                # so the pool stop-condition can detect it.
                report.verdict = "blocked"
                if (
                    is_unavailable_fallback
                    and result.failure is not None
                    and result.failure.reason != limit_trigger_reason
                ):
                    report.summary = (
                        f"{step} blocked after exhausting engine fallbacks following {limit_trigger_reason}: "
                        f"{result.failure.reason}"
                    )
                else:
                    report.summary = (
                        f"{step} blocked after exhausting engine fallbacks: {result.failure.reason}"
                    )
                report.feedback = cap_feedback(
                    "\n\n".join([*execution_events, result.transcript]).strip()
                )
                if not report.warnings or report.warnings[-1] != result.failure.reason:
                    report.warnings.append(result.failure.reason)
                return report
            post_stage_hook_report = _run_runner_hooks_for_stage(
                root,
                execution_root,
                current_task,
                step=step,
                config=config,
                phase="after",
                report=report,
            )
            if post_stage_hook_report is not None:
                return post_stage_hook_report
            post_accept_hook_report = _run_runner_hooks_for_stage(
                root,
                execution_root,
                current_task,
                step=step,
                config=config,
                phase="after_accept",
                report=report,
            )
            if post_accept_hook_report is not None:
                return post_accept_hook_report
            next_stage_engine_names = [engine_name]
            return report

        raise RuntimeError("Engine attempt order must include at least one engine")

    return executor
