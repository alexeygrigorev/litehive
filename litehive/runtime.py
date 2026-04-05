"""High-level runtime flow for executing queued tasks."""


from dataclasses import dataclass, field
import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import time
from typing import Callable

import yaml

from litehive.engines import extract_engine_continuation, get_engine
from litehive.config import ExecutionRetryPolicy, LitehiveConfig, load_config, load_context, state_path
from litehive.git_ops import (
    GitError,
    add_worktree,
    abort_revert,
    current_head,
    has_changes,
    is_git_repo,
    rebase_worktree_onto,
    rollback_message,
    rollback_task,
    status_porcelain,
)
from litehive.models import (
    RecoveryAction,
    RuntimeContinuationHandoff,
    StageReport,
    TaskThreadComment,
    TaskRecord,
    cap_feedback,
)
from litehive.runner import RunResult, StageExecutor, TaskExecutionRunner
from litehive.subagents import SubagentManager, stage_prompt, stage_report_from_subagent
from litehive.tasks import (
    _finalize_recovered_commit_task,
    _find_existing_checkpoint_commit,
    _atomic_write_gzip_text,
    _atomic_write_text,
    _workspace_lock,
    BlockedTask,
    append_thread_comment,
    collect_recovery_evidence,
    record_recovery_report,
    active_task_markers,
    append_journal,
    clear_task_outcome,
    clear_task_worktree_path,
    dequeue_next_task,
    dequeue_next_task_selection,
    get_task_worktree_path,
    get_task,
    implementation_entry_stage,
    list_tasks,
    load_state,
    mark_engine_switch,
    mark_task_run_started,
    peek_next_task,
    peek_next_task_selection,
    persist_task_and_state,
    prepare_completed_task_for_recovery,
    recover_stale_runner_state,
    restore_untouched_active_task,
    runner_heartbeat,
    set_pool_stop_reason,
    set_task_continuation_handoff,
    set_task_commit_sha,
    set_task_worktree_path,
    save_task,
    save_task_runtime,
    task_dir,
    task_file,
    task_runtime_file,
    workspace_mutation_guard,
    workspace_runner_guard,
    WorkspaceConflictError,
)

_COMPRESS_HOOK_ARTIFACT_MIN_BYTES = 4096


@dataclass(slots=True)
class ExecutionSummary:
    task: TaskRecord | None
    result: RunResult | None
    commit_sha: str | None = None


@dataclass(slots=True)
class TaskPoolRunSummary:
    executions: list[ExecutionSummary]
    stop_reason: str
    blocked: list[BlockedTask]


@dataclass(slots=True)
class SingleTaskRunSummary:
    execution: ExecutionSummary | None
    stop_reason: str
    blocked: list[BlockedTask]


@dataclass(slots=True)
class TaskPoolStopConditions:
    stop_on_failure: bool = False
    max_tasks: int | None = None
    stop_on_execution_limit: bool = False
    quota_threshold: int | None = None
    budget_threshold: int | None = None
    pool_usage_cap: int | None = None
    pool_cost_cap: int | None = None
    engine_usage_caps: dict[str, int] = field(default_factory=dict)
    engine_budget_caps: dict[str, int] = field(default_factory=dict)
    engine_costs: dict[str, int] = field(default_factory=dict)
    stop_on_dirty_git: bool = False


@dataclass(slots=True)
class RollbackSummary:
    task: TaskRecord
    rollback_sha: str
    rolled_back_sha: str


@dataclass(slots=True)
class DirtyWorktreeFinding:
    location_kind: str
    ownership: str
    dirty_paths: list[str] = field(default_factory=list)
    task_id: str | None = None
    worktree_path: str | None = None


@dataclass(slots=True)
class DirtyWorktreeGateReport:
    findings: list[DirtyWorktreeFinding] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.findings

    @property
    def blocks_pool(self) -> bool:
        return any(
            finding.ownership in {"main-checkout", "ambiguous-ownership", "missing-recorded-worktree"}
            for finding in self.findings
        )


@dataclass(frozen=True, slots=True)
class ResolvedExecutionRetryPolicy:
    selector: str
    policy: ExecutionRetryPolicy


@dataclass(slots=True)
class EngineBudgetLedger:
    pool_usage_cap: int | None = None
    pool_cost_cap: int | None = None
    engine_usage_caps: dict[str, int] = field(default_factory=dict)
    engine_budget_caps: dict[str, int] = field(default_factory=dict)
    engine_costs: dict[str, int] = field(default_factory=dict)
    total_usage: int = 0
    total_cost: int = 0
    usage_by_engine: dict[str, int] = field(default_factory=dict)
    cost_by_engine: dict[str, int] = field(default_factory=dict)

    def cost_for(self, engine_name: str) -> int:
        return self.engine_costs.get(engine_name, 1)

    def block_reason(self, engine_name: str) -> str | None:
        next_usage = self.total_usage + 1
        next_cost = self.total_cost + self.cost_for(engine_name)
        engine_usage = self.usage_by_engine.get(engine_name, 0) + 1
        engine_cost = self.cost_by_engine.get(engine_name, 0) + self.cost_for(engine_name)

        if self.pool_usage_cap is not None and next_usage > self.pool_usage_cap:
            return f"pool usage cap reached ({self.total_usage}/{self.pool_usage_cap})"
        if self.pool_cost_cap is not None and next_cost > self.pool_cost_cap:
            return f"pool cost cap reached ({self.total_cost}/{self.pool_cost_cap})"
        engine_usage_cap = self.engine_usage_caps.get(engine_name)
        if engine_usage_cap is not None and engine_usage > engine_usage_cap:
            return f"engine usage cap reached for `{engine_name}` ({self.usage_by_engine.get(engine_name, 0)}/{engine_usage_cap})"
        engine_budget_cap = self.engine_budget_caps.get(engine_name)
        if engine_budget_cap is not None and engine_cost > engine_budget_cap:
            return f"engine budget cap reached for `{engine_name}` ({self.cost_by_engine.get(engine_name, 0)}/{engine_budget_cap})"
        return None

    def record(self, engine_name: str) -> None:
        cost = self.cost_for(engine_name)
        self.total_usage += 1
        self.total_cost += cost
        self.usage_by_engine[engine_name] = self.usage_by_engine.get(engine_name, 0) + 1
        self.cost_by_engine[engine_name] = self.cost_by_engine.get(engine_name, 0) + cost

    def pool_stop_reason(self) -> str | None:
        if self.pool_usage_cap is not None and self.total_usage >= self.pool_usage_cap:
            return "pool_usage_cap_reached"
        if self.pool_cost_cap is not None and self.total_cost >= self.pool_cost_cap:
            return "pool_cost_cap_reached"
        return None


_PRE_STAGE_HOOK_POINTS = {
    "implementing": "before_swe_implementation",
    "accepting": "before_pm_acceptance",
}
_POST_STAGE_HOOK_POINTS = {
    "implementing": "after_swe_implementation",
}
_POST_ACCEPT_VERDICTS = {"pass", "accept"}


def resolve_next_task(root: Path) -> TaskRecord | None:
    root = root.resolve()
    return peek_next_task(root)


def run_next_task(root: Path) -> ExecutionSummary:
    root = root.resolve()
    task = dequeue_next_task(root)
    return run_task(root, task)


def run_task(
    root: Path,
    task: TaskRecord | None,
    *,
    engine_override: str | None = None,
    model_override: str | None = None,
    budget_ledger: EngineBudgetLedger | None = None,
) -> ExecutionSummary:
    root = root.resolve()
    if task is None:
        return ExecutionSummary(task=None, result=None)
    recover_stale_runner_state(root)
    markers = active_task_markers(root)
    if markers:
        active_ids = sorted(markers)
        if len(active_ids) > 1:
            details = "; ".join(
                f"{task_id} ({', '.join(markers[task_id])})" for task_id in active_ids
            )
            raise WorkspaceConflictError(
                f"workspace has multiple active tasks: {details}. Clear the stale active task state before running again."
            )
        active_id = active_ids[0]
        if active_id != task.id:
            raise WorkspaceConflictError(
                f"task {task.id} cannot start because task {active_id} is already active in this workspace."
            )
    with workspace_runner_guard(root):
        markers = active_task_markers(root)
        if markers:
            active_ids = sorted(markers)
            if len(active_ids) > 1:
                details = "; ".join(
                    f"{task_id} ({', '.join(markers[task_id])})" for task_id in active_ids
                )
                raise WorkspaceConflictError(
                    f"workspace has multiple active tasks: {details}. Clear the stale active task state before running again."
                )
            active_id = active_ids[0]
            if active_id != task.id:
                raise WorkspaceConflictError(
                    f"task {task.id} cannot start because task {active_id} is already active in this workspace."
                )

        config = load_config(root)
        workspace_context = load_context(root)
        engine_plan = resolve_engine_plan(task, config, engine_override=engine_override)
        engine_name = engine_plan[0]
        execution_root = _resolve_task_execution_root(root, task)
        subagents = SubagentManager(root, execution_root=execution_root)

        append_journal(root, task, f"Execution started with engine `{engine_name}`.")
        mark_task_run_started(root, task)
        retry_limit, retry_source = resolve_task_retry_policy(task, config)

        runner = TaskExecutionRunner(
            root,
            build_executor(
                root,
                execution_root=execution_root,
                initial_engine_names=engine_plan,
                workspace_context=workspace_context,
                subagents=subagents,
                config=config,
                task=task,
                model_override=model_override,
                config_auto_commit=config.auto_commit,
                budget_ledger=budget_ledger or _budget_ledger_from_config(config),
            ),
            max_retries=retry_limit,
            retry_source=retry_source,
            stage_retry_limit=_resolve_stage_retry_limit(task, config),
            subagents=subagents,
            config=config,
        )
        with runner_heartbeat(root, active_task_id=task.id):
            result = runner.run(task)
        if result.final_status != "done":
            append_journal(root, task, f"Execution finished with status `{result.final_status}`.")

        return ExecutionSummary(task=task, result=result, commit_sha=task.git.commit_sha)


def run_single_task(
    root: Path,
    *,
    engine_override: str | None = None,
    model_override: str | None = None,
    stop_conditions: TaskPoolStopConditions | None = None,
) -> SingleTaskRunSummary:
    root = root.resolve()
    with workspace_runner_guard(root):
        conditions = stop_conditions or TaskPoolStopConditions()
        budget_ledger = _budget_ledger_from_conditions(conditions)
        set_pool_stop_reason(root, None)

        stop_reason = _single_task_pre_stop_reason(
            root,
            stop_conditions=conditions,
            budget_ledger=budget_ledger,
        )
        if stop_reason is not None:
            set_pool_stop_reason(root, stop_reason)
            return SingleTaskRunSummary(execution=None, stop_reason=stop_reason, blocked=[])

        execution, blocked = run_next_task_with_override(
            root,
            engine_override=engine_override,
            model_override=model_override,
            budget_ledger=budget_ledger,
        )
        if execution.task is None:
            stop_reason = "blocked_tasks_remaining" if blocked else "queue_exhausted"
            set_pool_stop_reason(root, stop_reason)
            return SingleTaskRunSummary(execution=None, stop_reason=stop_reason, blocked=blocked)

        stop_reason = _single_task_stop_reason(execution)
        if stop_reason.startswith("human_checkpoint_"):
            set_pool_stop_reason(root, stop_reason)
        return SingleTaskRunSummary(execution=execution, stop_reason=stop_reason, blocked=blocked)


def drain_task_pool(
    root: Path,
    *,
    engine_override: str | None = None,
    model_override: str | None = None,
    stop_conditions: TaskPoolStopConditions | None = None,
    stop_when: Callable[[list[ExecutionSummary]], bool] | None = None,
) -> TaskPoolRunSummary:
    root = root.resolve()
    with workspace_runner_guard(root):
        executions: list[ExecutionSummary] = []
        conditions = stop_conditions or TaskPoolStopConditions()
        budget_ledger = _budget_ledger_from_conditions(conditions)
        set_pool_stop_reason(root, None)

        while True:
            stop_reason = _pool_stop_reason(root, executions, conditions, budget_ledger=budget_ledger)
            if stop_reason is not None:
                blocked: list[BlockedTask] = []
                if stop_reason == "blocked_tasks_remaining":
                    blocked = peek_next_task_selection(root).blocked
                return _finalize_pool_run(
                    root, executions=executions, stop_reason=stop_reason, blocked=blocked
                )
            if stop_when is not None and stop_when(executions):
                return _finalize_pool_run(
                    root,
                    executions=executions,
                    stop_reason="stop_condition_reached",
                    blocked=[],
                )
            execution, blocked = run_next_task_with_override(
                root,
                engine_override=engine_override,
                model_override=model_override,
                budget_ledger=budget_ledger,
            )
            if execution.task is None:
                stop_reason = "blocked_tasks_remaining" if blocked else "queue_exhausted"
                return _finalize_pool_run(
                    root, executions=executions, stop_reason=stop_reason, blocked=blocked
                )
            executions.append(execution)


def run_next_task_with_override(
    root: Path,
    *,
    engine_override: str | None = None,
    model_override: str | None = None,
    budget_ledger: EngineBudgetLedger | None = None,
) -> tuple[ExecutionSummary, list[BlockedTask]]:
    root = root.resolve()
    selection = dequeue_next_task_selection(root)
    return (
        run_task(
            root,
            selection.task,
            engine_override=engine_override,
            model_override=model_override,
            budget_ledger=budget_ledger,
        ),
        selection.blocked,
    )


def _pool_stop_reason(
    root: Path,
    executions: list[ExecutionSummary],
    conditions: TaskPoolStopConditions,
    *,
    budget_ledger: EngineBudgetLedger | None = None,
) -> str | None:
    if conditions.stop_on_dirty_git and _git_worktree_blocks_pool(root):
        return "dirty_git_state"
    if conditions.max_tasks is not None and len(executions) >= conditions.max_tasks:
        return "max_tasks_reached"
    if budget_ledger is not None:
        budget_stop_reason = budget_ledger.pool_stop_reason()
        if budget_stop_reason is not None:
            return budget_stop_reason
    if not executions:
        return None

    latest = executions[-1]
    if latest.result is not None and latest.result.final_status == "paused":
        return _human_checkpoint_stop_reason(latest)
    if latest.result is not None and latest.result.final_status == "queued":
        if conditions.stop_on_failure:
            return "failure_detected"
        selection = peek_next_task_selection(root)
        if selection.task is not None:
            return None
        return "blocked_tasks_remaining" if selection.blocked else "queue_exhausted"
    if latest.result is not None and latest.result.final_status == "interrupted":
        return "task_interrupted"
    if _requires_continue_or_rollback(root, latest):
        return "continue_or_rollback_required"
    latest_limit_kind = _execution_limit_kind(latest)
    final_status = latest.result.final_status if latest.result is not None else None
    if conditions.stop_on_failure and final_status is not None and final_status not in {"done", "paused"}:
        return "failure_detected"
    if conditions.stop_on_execution_limit and _execution_hit_limit(latest):
        return "execution_limit_reached"
    if (
        latest_limit_kind == "quota"
        and conditions.quota_threshold is not None
        and _count_execution_limits(executions, kind="quota") >= conditions.quota_threshold
    ):
        return "quota_threshold_reached"
    if (
        latest_limit_kind == "budget"
        and conditions.budget_threshold is not None
        and _count_execution_limits(executions, kind="budget") >= conditions.budget_threshold
    ):
        return "budget_threshold_reached"
    if _execution_exhausted_limit_fallbacks(latest) and not _limit_stop_condition_is_configured(
        conditions,
        latest_limit_kind,
    ):
        return "execution_limit_fallbacks_exhausted"
    return None


def _single_task_pre_stop_reason(
    root: Path,
    *,
    stop_conditions: TaskPoolStopConditions,
    budget_ledger: EngineBudgetLedger,
) -> str | None:
    if stop_conditions.stop_on_dirty_git and _git_worktree_blocks_pool(root):
        return "dirty_git_state"
    return budget_ledger.pool_stop_reason()


def _single_task_stop_reason(execution: ExecutionSummary) -> str:
    result = execution.result
    if result is not None and result.final_status == "paused":
        return _human_checkpoint_stop_reason(execution)
    if result is not None and result.final_status == "queued":
        return "task_requeued"
    if result is not None and result.final_status == "interrupted":
        return "task_interrupted"
    return "single_task_complete"


def _requires_continue_or_rollback(root: Path, execution: ExecutionSummary) -> bool:
    task = execution.task
    result = execution.result
    if task is None or result is None:
        return False
    if result.final_status != "done":
        return False
    if execution.commit_sha is None:
        return False
    state = load_state(root)
    return bool(state.queue)


def _git_worktree_is_dirty(root: Path) -> bool:
    return is_git_repo(root) and has_changes(root)


def _git_worktree_blocks_pool(root: Path) -> bool:
    return inspect_dirty_worktree_gate(root).blocks_pool


def _dirty_worktree_owner_task(root: Path) -> TaskRecord | None:
    report = inspect_dirty_worktree_gate(root)
    task_ids = [
        finding.task_id
        for finding in report.findings
        if finding.location_kind == "main-checkout" and finding.ownership == "task-owned" and finding.task_id
    ]
    if len(task_ids) != 1:
        return None
    return get_task(root, task_ids[0])


def inspect_dirty_worktree_gate(root: Path) -> DirtyWorktreeGateReport:
    if not is_git_repo(root):
        return DirtyWorktreeGateReport()

    findings: list[DirtyWorktreeFinding] = []
    try:
        dirty_entries = status_porcelain(root)
    except GitError:
        return DirtyWorktreeGateReport()

    tasks = list_tasks(root)
    if dirty_entries:
        owners = [
            task
            for task in tasks
            if _task_can_resume_with_owned_dirty_paths(root, task, dirty_entries)
        ]
        finding = DirtyWorktreeFinding(
            location_kind="main-checkout",
            ownership="main-checkout",
            dirty_paths=_dirty_entry_paths(dirty_entries),
        )
        if len(owners) == 1:
            finding.ownership = "task-owned"
            finding.task_id = owners[0].id
            finding.worktree_path = get_task_worktree_path(owners[0])
        elif len(owners) > 1:
            finding.ownership = "ambiguous-ownership"
            finding.task_id = ",".join(task.id for task in owners)
        findings.append(finding)

    for task in tasks:
        worktree_path = get_task_worktree_path(task)
        if not worktree_path:
            continue
        resolved_path = (root / worktree_path).resolve()
        if not resolved_path.exists():
            findings.append(
                DirtyWorktreeFinding(
                    location_kind="task-worktree",
                    ownership="missing-recorded-worktree",
                    task_id=task.id,
                    worktree_path=worktree_path,
                )
            )
            continue
        try:
            worktree_dirty_entries = status_porcelain(resolved_path)
        except GitError:
            findings.append(
                DirtyWorktreeFinding(
                    location_kind="task-worktree",
                    ownership="missing-recorded-worktree",
                    task_id=task.id,
                    worktree_path=worktree_path,
                )
            )
            continue
        if not worktree_dirty_entries:
            continue
        findings.append(
            DirtyWorktreeFinding(
                location_kind="task-worktree",
                ownership="task-owned-worktree",
                task_id=task.id,
                worktree_path=worktree_path,
                dirty_paths=_dirty_entry_paths(worktree_dirty_entries),
            )
        )

    return DirtyWorktreeGateReport(findings=findings)


def _task_can_resume_with_owned_dirty_paths(
    root: Path,
    task: TaskRecord,
    dirty_entries: list[str],
) -> bool:
    if task.status != "interrupted":
        return False
    if task.pipeline_status in {"backlog", "done"}:
        return False
    return not _unexpected_dirty_paths(dirty_entries, _allowed_commit_paths(root, task))


def _task_worktree_path(root: Path, task: TaskRecord) -> Path:
    return root / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"


def _resolve_task_execution_root(root: Path, task: TaskRecord) -> Path:
    if not is_git_repo(root):
        return root

    worktree_path_value = get_task_worktree_path(task)
    if worktree_path_value:
        worktree_path = (root / worktree_path_value).resolve()
        if not worktree_path.exists():
            # Worktree was deleted (manual cleanup or prior crash) — clear stale ref and recreate below
            set_task_worktree_path(task, None)
            save_task(root, task)
        else:
            main_head = current_head(root)
            if main_head:
                rebase_worktree_onto(worktree_path, main_head)
            return worktree_path

    worktree_path = _task_worktree_path(root, task)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        shutil.rmtree(worktree_path)
    add_worktree(root, worktree_path, ref=current_head(root) or "HEAD")
    set_task_worktree_path(task, str(worktree_path.relative_to(root)))
    save_task(root, task)
    append_journal(root, task, f"Created task worktree at `{get_task_worktree_path(task)}`.")
    return worktree_path




def _path_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _traceback_text(failed_report: StageReport) -> str:
    traceback_text = failed_report.failure_diagnostics.get("traceback")
    if isinstance(traceback_text, str) and traceback_text.strip():
        return traceback_text
    feedback = failed_report.feedback or ""
    return feedback if "Traceback" in feedback else ""


def _traceback_frame_paths(traceback_text: str) -> list[Path]:
    return [Path(match) for match in re.findall(r'File "([^"]+)"', traceback_text)]


def _traceback_fingerprint(traceback_text: str, summary: str) -> str:
    signature_lines = [
        line.strip()
        for line in traceback_text.splitlines()
        if line.strip().startswith('File "') or line.strip().startswith(("raise ", "AssertionError", "RuntimeError", "ValueError", "TypeError"))
    ]
    payload = "\n".join(signature_lines) or summary
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _classify_recovery_failure_owner(
    root: Path,
    failed_report: StageReport,
    *,
    config: LitehiveConfig | None,
) -> tuple[str, str, Path | None]:
    traceback_text = _traceback_text(failed_report)
    if not traceback_text:
        return "unknown", "", None
    frame_paths = _traceback_frame_paths(traceback_text)
    source_root = None
    if config and config.litehive_source_path:
        source_root = Path(config.litehive_source_path).expanduser().resolve()
    for frame in frame_paths:
        if source_root is not None and _path_within(frame, source_root):
            return "litehive", traceback_text, source_root
        if _path_within(frame, root):
            return "project", traceback_text, source_root
        normalized = frame.as_posix()
        if "/site-packages/litehive/" in normalized or normalized.endswith("/litehive/__init__.py") or "/litehive/" in normalized:
            return "litehive", traceback_text, source_root
    return "unknown", traceback_text, source_root










def _attempt_stage_recovery(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    step: str,
    failed_report: StageReport,
    *,
    subagents: SubagentManager | None = None,
    config: LitehiveConfig | None = None,
    role_name: str = "swe",
    engine_name: str = "codex",
    model_name: str | None = None,
) -> StageReport | None:
    """Launch a recovery agent after a stage failure. Returns a new report or None."""
    if subagents is None:
        return None

    evidence = collect_recovery_evidence(root, task, stage=step)
    evidence_lines = "\n".join(
        f"- {item.label}: {item.path or 'n/a'} ({'present' if item.exists else 'missing'}) :: {item.summary}"
        for item in evidence
    )
    failure_owner, traceback_text, source_root = _classify_recovery_failure_owner(
        root,
        failed_report,
        config=config,
    )
    append_journal(
        root, task,
        f"Stage `{step}` {failed_report.verdict}: {failed_report.summary}. Launching recovery agent.",
    )
    prompt = (
        f"You are running as Litehive's recovery agent for task {task.id} ({task.title}).\n\n"
        f"Failure trigger: stage `{step}` ended with verdict `{failed_report.verdict}`.\n"
        f"Failure summary: {failed_report.summary}\n\n"
        f"Failure ownership classification: {failure_owner}\n\n"
        f"Previous report feedback:\n{failed_report.feedback or '(none)'}\n\n"
        f"Working directory: {execution_root}\n\n"
        f"Recovery evidence gathered automatically:\n{evidence_lines}\n\n"
        f"Bounded recovery policy:\n"
        f"- gather enough evidence to classify the failure\n"
        f"- apply only the smallest safe repair needed to restore a runnable path\n"
        f"- prefer fixing continuation state, engine bindings, prompts, or task-local state over broad refactors\n"
        f"- if the task is underspecified, leave explicit notes and keep it runnable for planner/grooming\n\n"
        f"Acceptance criteria:\n"
        + "\n".join(f"- {c}" for c in task.acceptance_criteria)
        + f"\n\nWhen you finish, submit a detailed report with `litehive report`.\n"
        f"Use verdict `pass` only if the task is runnable again or the current stage is now complete.\n"
        f"Use verdict `blocked` or `fail` if a blocker remains.\n"
    )

    recovery_result = subagents.run(
        task,
        role="recovery",
        engine_name=engine_name,
        prompt=prompt,
        model=model_name,
    )

    # Check if the recovery agent submitted a verdict via thread
    from litehive.tasks import load_task_thread

    thread = load_task_thread(root, task)
    recovery_comments = [
        c for c in thread
        if c.step == step and c.verdict in ("pass", "accept")
    ]
    if recovery_comments:
        latest = recovery_comments[-1]
        record_recovery_report(
            root,
            task,
            trigger="stage_failure",
            stage=step,
            summary=latest.message.splitlines()[0] if latest.message else f"{step} recovered",
            runnable_state="runnable",
            failure_classification=failed_report.failure_classification or failed_report.verdict,
            actions=[
                RecoveryAction(
                    action="resume_current_stage",
                    summary=f"Recovery agent repaired the task and returned `{step}` to a runnable state.",
                    metadata={"verdict": latest.verdict},
                )
            ],
            warnings=list(failed_report.warnings),
            recovery_subagent_id=recovery_result.ref.id,
            recovery_subagent_path=recovery_result.ref.path,
        )
        append_journal(root, task, f"Recovery agent resolved {step}: {latest.verdict}")
        return StageReport(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            verdict=latest.verdict,  # type: ignore[arg-type]
            summary=latest.message.splitlines()[0] if latest.message else f"{step} recovered",
            feedback=latest.message,
            files_changed=latest.files_changed,
        )

    # Fallback: check if recovery agent produced a passing report via stdout
    recovery_report = stage_report_from_subagent(task, step, recovery_result, root=root)
    if recovery_report.verdict in ("pass", "accept"):
        record_recovery_report(
            root,
            task,
            trigger="stage_failure",
            stage=step,
            summary=recovery_report.summary,
            runnable_state="runnable",
            failure_classification=failed_report.failure_classification or failed_report.verdict,
            actions=[
                RecoveryAction(
                    action="resume_current_stage",
                    summary=f"Recovery agent repaired the task and returned `{step}` to a runnable state.",
                    metadata={"verdict": recovery_report.verdict},
                )
            ],
            warnings=[*failed_report.warnings, *recovery_report.warnings],
            recovery_subagent_id=recovery_result.ref.id,
            recovery_subagent_path=recovery_result.ref.path,
        )
        append_journal(root, task, f"Recovery agent resolved {step}: {recovery_report.verdict}")
        return recovery_report

    blocker = recovery_report.summary or failed_report.summary
    record_recovery_report(
        root,
        task,
        trigger="stage_failure",
        stage=step,
        summary=f"Recovery agent could not restore `{step}` to a runnable state.",
        runnable_state="blocked",
        failure_classification=failed_report.failure_classification or failed_report.verdict,
        blocker=blocker,
        actions=[
            RecoveryAction(
                action="no_safe_repair",
                applied=False,
                summary="Recovery agent investigated the failure but could not apply a safe bounded repair.",
            )
        ],
        warnings=[*failed_report.warnings, *recovery_report.warnings],
        recovery_subagent_id=recovery_result.ref.id,
        recovery_subagent_path=recovery_result.ref.path,
    )
    append_journal(root, task, f"Recovery agent could not resolve {step}.")
    return None




def _resolve_recovery_engine(
    task: TaskRecord, config: LitehiveConfig | None,
) -> tuple[str, str | None]:
    """Return (engine_name, model_name) for recovery/merge-resolver agents."""
    if config and config.recovery_engine:
        engine = config.recovery_engine
    else:
        engine = task.engine or (config.default_engine if config else "codex")
    model = resolve_model(task, config, engine_name=engine) if config else None
    return engine, model


















def _execution_hit_limit(execution: ExecutionSummary) -> bool:
    return _execution_limit_kind(execution) is not None


def _execution_exhausted_limit_fallbacks(execution: ExecutionSummary) -> bool:
    task = execution.task
    if task is None:
        return False
    outcome = task.runtime.last_outcome
    if outcome.kind != "blocked":
        return False
    reason = outcome.reason.lower()
    return "exhausting engine fallbacks" in reason and _execution_limit_kind(execution) is not None


def _execution_limit_kind(execution: ExecutionSummary) -> str | None:
    task = execution.task
    if task is None:
        return None
    outcome = task.runtime.last_outcome
    if outcome.kind != "blocked":
        return None
    reason = outcome.reason.lower()
    if any(marker in reason for marker in ("budget", "credit", "insufficient funds")):
        return "budget"
    if any(
        marker in reason
        for marker in (
            "quota",
            "usage limit",
            "capacity limit",
            "rate limit",
            "too many requests",
        )
    ):
        return "quota"
    return None


def _count_execution_limits(executions: list[ExecutionSummary], *, kind: str) -> int:
    return sum(1 for execution in executions if _execution_limit_kind(execution) == kind)


def _limit_stop_condition_is_configured(
    conditions: TaskPoolStopConditions,
    kind: str | None,
) -> bool:
    if kind == "quota":
        return conditions.stop_on_execution_limit or conditions.quota_threshold is not None
    if kind == "budget":
        return conditions.stop_on_execution_limit or conditions.budget_threshold is not None
    return False


def _finalize_pool_run(
    root: Path,
    *,
    executions: list[ExecutionSummary],
    stop_reason: str,
    blocked: list[BlockedTask],
) -> TaskPoolRunSummary:
    restore_untouched_active_task(root)
    set_pool_stop_reason(root, stop_reason)
    if stop_reason == "execution_limit_fallbacks_exhausted" and executions:
        latest = executions[-1]
        if latest.task is not None:
            outcome_reason = latest.task.runtime.last_outcome.reason or "engine fallbacks exhausted"
            append_journal(root, latest.task, f"Pool stopped: {stop_reason}. {outcome_reason}")
    if stop_reason.startswith("human_checkpoint_") and executions:
        latest = executions[-1]
        if latest.task is not None:
            checkpoint = stop_reason.removeprefix("human_checkpoint_").replace("_", " ")
            append_journal(root, latest.task, f"Pool stopped: {stop_reason}. Awaiting human review at {checkpoint}.")
    if stop_reason == "continue_or_rollback_required" and executions:
        latest = executions[-1]
        if latest.task is not None and latest.commit_sha is not None:
            append_journal(
                root,
                latest.task,
                (
                    "Pool stopped: continue_or_rollback_required. "
                    "This task finished with checkpoint commit "
                    f"`{latest.commit_sha}` and unrelated queued work remains. "
                    "Either continue with a new `litehive run`/pool run or roll back the checkpoint first."
                ),
            )
    return TaskPoolRunSummary(executions=executions, stop_reason=stop_reason, blocked=blocked)


def _human_checkpoint_stop_reason(execution: ExecutionSummary) -> str:
    task = execution.task
    if task is None:
        return "human_checkpoint_reached"
    if task.pipeline_status == "accepting":
        return "human_checkpoint_before_acceptance"
    if task.pipeline_status == "commit_to_git":
        return "human_checkpoint_before_commit"
    return "human_checkpoint_reached"


def rollback_completed_task(root: Path, task_id: str) -> RollbackSummary:
    root = root.resolve()
    with workspace_mutation_guard(root), _workspace_lock(root):
        task = get_task(root, task_id)
        if task is None:
            raise GitError(f"Task {task_id} not found")
        _require_completed_task(task, action="rollback")

        attempt = task.git.checkpoint_attempts
        recovery_stage = implementation_entry_stage(task)
        state = load_state(root)
        snapshot = _capture_persisted_files(
            [
                task_file(root, task),
                task_runtime_file(root, task),
                state_path(root),
                task_dir(root, task) / "journal.md",
            ]
        )
        rollback = None
        try:
            rollback = rollback_task(root, task)
            prepare_completed_task_for_recovery(task, recovery_stage=recovery_stage)
            task.git.rolled_back_checkpoint_attempt = attempt
            state.active_task_id = None
            state.queue = [item for item in state.queue if item != task.id]
            state.queue.append(task.id)
            persist_task_and_state(
                root,
                task=task,
                state=state,
                journal_message=(
                    "Checkpoint rollback requested.\n"
                    f"- rolled_back_attempt: `{attempt}`\n"
                    f"- recovery_stage: `{recovery_stage}`"
                ),
            )
            rollback_checkpoint = commit_task(root, rollback_message(task, attempt))
            if rollback_checkpoint is None:
                raise GitError("git rollback commit failed")
        except Exception:
            if rollback is not None and has_changes(root):
                abort_revert(root)
            _restore_persisted_files(snapshot)
            raise
        return RollbackSummary(
            task=task,
            rollback_sha=rollback_checkpoint.commit_sha,
            rolled_back_sha=rollback.rolled_back_sha,
        )


def recover_completed_task(root: Path, task_id: str) -> TaskRecord:
    root = root.resolve()
    with workspace_mutation_guard(root), _workspace_lock(root):
        task = get_task(root, task_id)
        if task is None:
            raise GitError(f"Task {task_id} not found")
        _require_completed_task(task, action="recover")

        recovery_stage = implementation_entry_stage(task)
        prepare_completed_task_for_recovery(task, recovery_stage=recovery_stage)
        state = load_state(root)
        state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        state.queue.append(task.id)
        persist_task_and_state(
            root,
            task=task,
            state=state,
            journal_message="Task recovered for another implementation pass.",
        )
        return task


def _capture_persisted_files(paths: list[Path]) -> dict[Path, str | None]:
    return {
        path: path.read_text(encoding="utf-8") if path.exists() else None
        for path in paths
    }


def _restore_persisted_files(snapshot: dict[Path, str | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            if path.exists():
                path.unlink()
            continue
        _atomic_write_text(path, content)


def _is_recovery_run(task: TaskRecord) -> bool:
    if task.runtime.continuation_handoff is not None:
        return True
    return task.runtime.last_outcome.kind in {"flagged", "interrupted"}


def _role_for_step(step: str, task: TaskRecord | None = None) -> str:
    if task is not None and step in {"implementing", "testing", "accepting"} and _is_recovery_run(task):
        return "recovery"
    return {
        "grooming": "planner",
        "implementing": "swe",
        "testing": "qa",
        "accepting": "reviewer",
    }.get(step, "swe")


def resolve_engine_name(
    task: TaskRecord,
    config: LitehiveConfig,
    *,
    engine_override: str | None = None,
) -> str:
    return resolve_engine_plan(task, config, engine_override=engine_override)[0]


def resolve_engine_attempt_order(
    task: TaskRecord,
    config: LitehiveConfig,
    *,
    engine_override: str | None = None,
) -> list[str]:
    return _engine_attempt_order(
        resolve_engine_plan(task, config, engine_override=engine_override),
        config.engine_fallbacks,
    )


def resolve_engine_plan(
    task: TaskRecord,
    config: LitehiveConfig,
    *,
    engine_override: str | None = None,
) -> list[str]:
    if engine_override is not None:
        return [engine_override]
    if task.engine is not None:
        return [task.engine]
    routed_engines = _route_engines_for_task(task, config)
    if routed_engines:
        return routed_engines
    return [config.default_engine]


def _route_engines_for_task(task: TaskRecord, config: LitehiveConfig) -> list[str]:
    route_key = _task_routing_key(task)
    if route_key is None:
        return []

    engines: list[str] = []
    seen: set[str] = set()
    for engine_name in config.task_engine_routing.get(route_key, []):
        if engine_name in seen:
            continue
        seen.add(engine_name)
        engines.append(engine_name)
    return engines


def _task_routing_key(task: TaskRecord) -> str | None:
    if task.task_type is not None:
        return task.task_type

    text = " ".join(
        [
            task.title,
            task.goal,
            *task.acceptance_criteria,
            *task.constraints,
            *task.plan,
        ]
    ).lower()
    if not text.strip():
        return None

    routing_patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("adapter", ("adapter", "integration", "provider", "cli adapter", "engine adapter")),
        ("bugfix", ("bugfix", "bug fix", "fix ", "fixes ", "fixing ", "regression", "hotfix", "flaky")),
        ("review", ("review", "review feedback", "requested changes", "triage", "comments")),
        ("research", ("research", "investigate", "investigation", "spike", "analyze", "analysis", "explore", "evaluate")),
        ("refactor", ("refactor", "cleanup", "clean up", "rename", "reorganize", "extract", "simplify")),
        ("docs", (" docs", "doc ", "documentation", "document ", "readme", "guide")),
    )
    padded = f" {text} "
    for route_key, patterns in routing_patterns:
        if any(pattern in padded for pattern in patterns):
            return route_key
    if re.search(r"\bfix\b", text):
        return "bugfix"
    if re.search(r"\bdoc(s)?\b", text):
        return "docs"
    return None

def resolve_task_retry_policy(task: TaskRecord, config: LitehiveConfig) -> tuple[int, str]:
    if task.retry_policy.max_retries is not None:
        return task.retry_policy.max_retries, "task"
    return config.default_retry_limit, "global"


def _resolve_stage_retry_limit(task: TaskRecord, config: LitehiveConfig) -> int:
    if task.retry_policy.stage_retry_limit is not None:
        return task.retry_policy.stage_retry_limit
    return config.default_stage_retry_limit


def _execution_retry_model_family(*, engine_name: str, model_name: str | None) -> str:
    if model_name:
        model_tail = model_name.rsplit("/", 1)[-1].strip().lower()
        match = re.match(r"[a-z0-9]+", model_tail)
        if match is not None:
            return match.group(0)
    return engine_name


def resolve_execution_retry_policy(
    config: LitehiveConfig, *, engine_name: str, model_name: str | None = None
) -> ResolvedExecutionRetryPolicy:
    model_family = _execution_retry_model_family(engine_name=engine_name, model_name=model_name)
    selector_order = [engine_name, f"model_family:{model_family}", "external_cli"]
    for selector in selector_order:
        if selector in config.execution_retry_policies:
            return ResolvedExecutionRetryPolicy(
                selector=selector,
                policy=config.execution_retry_policies[selector],
            )
    return ResolvedExecutionRetryPolicy(selector="none", policy=ExecutionRetryPolicy())


def _retry_backoff_seconds(policy: ExecutionRetryPolicy, retry_number: int) -> float:
    if retry_number <= 0:
        return 0.0
    return policy.backoff_seconds * (policy.backoff_multiplier ** (retry_number - 1))


def _set_continuation_handoff(
    root: Path,
    task: TaskRecord,
    *,
    step: str,
    kind: str,
    reason: str,
    result,
    from_engine: str,
    to_engine: str | None,
    from_model: str | None,
    to_model: str | None,
    attempt: int,
) -> RuntimeContinuationHandoff:
    transcript_snippet = ""
    summary = ""
    warnings: list[str] = []
    if result.transcript:
        transcript_snippet = result.transcript.splitlines()[0].strip()
    if result.execution is not None:
        rendered = get_engine(from_engine).render_transcript(result.execution)
        transcript_snippet = transcript_snippet or rendered.splitlines()[0].strip()
        if rendered.strip():
            report = get_engine(from_engine).parse_stage_report(
                task_id=task.id,
                step=step,  # type: ignore[arg-type]
                execution=result.execution,
                subagent_status=result.ref.status,
            )
            summary = report.summary
            warnings = list(report.warnings)

    handoff = RuntimeContinuationHandoff(
        step=step,
        kind=kind,  # type: ignore[arg-type]
        reason=reason,
        from_engine=from_engine,
        to_engine=to_engine,
        from_model=from_model,
        to_model=to_model,
        subagent_id=result.ref.id,
        subagent_path=result.ref.path,
        status=result.ref.status,
        attempt=attempt,
        summary=summary,
        transcript_snippet=transcript_snippet,
        warnings=warnings,
        session_path=f"{result.ref.path}/session.yaml",
        report_path=f"{result.ref.path}/report.yaml",
        transcript_path=f"{result.ref.path}/transcript.md",
        continuation=extract_engine_continuation(from_engine, result.execution),
    )
    set_task_continuation_handoff(root, task, handoff)
    return handoff


def _require_completed_task(task: TaskRecord, action: str) -> None:
    if task.status != "done" or task.pipeline_status != "done":
        raise GitError(f"Task {task.id} is not completed; cannot {action}")


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
        engines = _engine_attempt_order(next_stage_engine_names, config.engine_fallbacks)
        limit_trigger_reason: str | None = None

        for index, engine_name in enumerate(engines):
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
            model_name = resolve_model(task, config, engine_name=engine_name, model_override=model_override)
            retry_policy = resolve_execution_retry_policy(
                config,
                engine_name=engine_name,
                model_name=model_name,
            )
            max_turns = config.claude_max_turns if engine_name == "claude" else None
            attempt_count = 0
            retry_exhausted_reason: str | None = None
            resume_session_id: str | None = None
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
            if (is_limit_failure or is_unavailable_fallback or is_retry_exhausted_failure) and index + 1 < len(engines):
                next_engine = engines[index + 1]
                failure_reason = result.failure.reason if result.failure is not None else retry_exhausted_reason
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
            from litehive.engines import extract_engine_continuation

            thread_comments = [
                c for c in load_task_thread(root, current_task)
                if c.step == step and c.verdict != "comment"
            ]
            if (
                not thread_comments
                and result.failure is None
                and engine_name == "claude"
                and result.execution is not None
            ):
                continuation = extract_engine_continuation(engine_name, result.execution)
                if continuation and continuation.session_id:
                    nudge_prompt = (
                        f"You finished the {step} stage but did not submit your verdict. "
                        f"Please run this command now:\n\n"
                        f"  litehive report --verdict <pass|fail|reject> --role {role_name} "
                        f"--step {step} --message \"<your detailed report>\"\n\n"
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
            prior_attempts = len(list(_reports_dir.glob(f"{step}-*.yaml"))) if _reports_dir.exists() else 0
            is_engine_break = (
                result.failure is not None
                and result.failure.kind in ("retryable_execution_error", "engine_error")
                and step != "commit_to_git"
            )
            is_stuck_loop = (
                report.verdict in ("fail", "reject")
                and step in ("implementing", "testing")
                and prior_attempts >= 3  # 3+ prior attempts = stuck
            )
            if is_engine_break or is_stuck_loop:
                recovered_report = _attempt_stage_recovery(
                    root, execution_root, current_task, step, report,
                    subagents=subagents, config=config,
                    role_name=role_name, engine_name=engine_name, model_name=model_name,
                )
                if recovered_report is not None:
                    report = recovered_report

            if execution_events:
                report.warnings = [*execution_events, *report.warnings]
                report.feedback = cap_feedback("\n\n".join([*execution_events, report.feedback]).strip())
            _attach_runner_hook_results(report, pre_stage_hook_results)
            if limit_trigger_reason is not None and (is_limit_failure or is_unavailable_fallback):
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
                report.feedback = cap_feedback("\n\n".join([*execution_events, result.transcript]).strip())
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


def workspace_model_for_engine(config: LitehiveConfig, engine_name: str) -> str | None:
    if engine_name == "codex":
        return config.codex_model
    if engine_name == "opencode":
        return config.opencode_model
    if engine_name == "goz":
        return config.goz_model
    if engine_name == "gemini":
        return config.gemini_model
    if engine_name == "copilot":
        return config.copilot_model
    if engine_name == "claude":
        return config.claude_model
    return None


def resolve_model(
    task: TaskRecord,
    config: LitehiveConfig,
    *,
    engine_name: str,
    model_override: str | None = None,
) -> str | None:
    if not get_engine(engine_name).capabilities.supports_model_override:
        return None
    if model_override is not None:
        return model_override
    if task.model is not None:
        return task.model
    return workspace_model_for_engine(config, engine_name)


def _budget_ledger_from_config(config: LitehiveConfig) -> EngineBudgetLedger:
    return EngineBudgetLedger(
        pool_usage_cap=config.pool_usage_cap,
        pool_cost_cap=config.pool_cost_cap,
        engine_usage_caps=dict(config.engine_usage_caps),
        engine_budget_caps=dict(config.engine_budget_caps),
        engine_costs=dict(config.engine_costs),
    )


def _budget_ledger_from_conditions(conditions: TaskPoolStopConditions) -> EngineBudgetLedger:
    return EngineBudgetLedger(
        pool_usage_cap=conditions.pool_usage_cap,
        pool_cost_cap=conditions.pool_cost_cap,
        engine_usage_caps=dict(conditions.engine_usage_caps),
        engine_budget_caps=dict(conditions.engine_budget_caps),
        engine_costs=dict(conditions.engine_costs),
    )


def _engine_attempt_order(
    initial_engine_names: list[str], engine_fallbacks: dict[str, list[str]]
) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    queue: list[str] = list(initial_engine_names)
    while queue:
        engine_name = queue.pop(0)
        if engine_name in seen:
            continue
        seen.add(engine_name)
        ordered.append(engine_name)
        queue.extend(engine_fallbacks.get(engine_name, []))
    return ordered


def _run_runner_hooks_for_stage(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    *,
    step: str,
    config: LitehiveConfig,
    phase: str,
    report: StageReport | None = None,
    collected_results: list[dict[str, str | int | bool | None]] | None = None,
) -> StageReport | None:
    hook_point = _runner_hook_point(step=step, phase=phase, report=report)
    if hook_point is None:
        return None
    configured_hooks = config.runner_hooks.get(hook_point, [])
    if not configured_hooks:
        return None

    for index, hook in enumerate(configured_hooks, start=1):
        hook_result = _execute_runner_hook(
            root,
            execution_root,
            task,
            step=step,
            hook_point=hook_point,
            command=hook.command,
            blocking=hook.blocking,
            ordinal=index,
            legacy_artifact_name=(
                "pre-acceptance-hook.txt"
                if hook_point == "before_pm_acceptance"
                and config.pre_acceptance_command == hook.command
                else None
            ),
        )
        if report is None:
            if collected_results is not None:
                collected_results.append(hook_result)
            if hook_result["status"] == "failed" and hook.blocking:
                blocking_results = list(collected_results or [hook_result])
                return StageReport(
                    task_id=task.id,
                    step=step,  # type: ignore[arg-type]
                    verdict="blocked",
                    summary=(
                        f"{step} blocked by runner hook `{hook_point}` "
                        f"(exit {hook_result['exit_code']}): {hook.command}"
                    ),
                    feedback="\n\n".join(_flatten_runner_hook_feedback(blocking_results)),
                    warnings=_flatten_runner_hook_warnings(blocking_results),
                    hook_results=blocking_results,
                )
            continue

        report.hook_results.append(hook_result)
        report.warnings = [*report.warnings, *_runner_hook_warnings(hook_result)]
        report.feedback = "\n\n".join(
            part for part in [report.feedback, _runner_hook_feedback(hook_result)] if part
        ).strip()
        if hook_result["status"] == "failed" and hook.blocking:
            report.verdict = "blocked"
            report.summary = (
                f"{step} blocked by runner hook `{hook_point}` "
                f"(exit {hook_result['exit_code']}): {hook.command}"
            )
            return report

    return None


def _attach_runner_hook_results(
    report: StageReport,
    hook_results: list[dict[str, str | int | bool | None]],
) -> None:
    if not hook_results:
        return
    report.hook_results.extend(hook_results)
    report.warnings = [
        *_flatten_runner_hook_warnings(hook_results),
        *report.warnings,
    ]
    report.feedback = cap_feedback("\n\n".join(
        [
            *_flatten_runner_hook_feedback(hook_results),
            report.feedback,
        ]
    ).strip())


def _runner_hook_point(
    *,
    step: str,
    phase: str,
    report: StageReport | None,
) -> str | None:
    if phase == "before":
        return _PRE_STAGE_HOOK_POINTS.get(step)
    if phase == "after":
        return _POST_STAGE_HOOK_POINTS.get(step)
    if phase == "after_accept" and step == "accepting" and report is not None:
        if report.verdict in _POST_ACCEPT_VERDICTS:
            return "after_pm_acceptance"
    return None


def _execute_runner_hook(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    *,
    step: str,
    hook_point: str,
    command: str,
    blocking: bool,
    ordinal: int,
    legacy_artifact_name: str | None,
) -> dict[str, str | int | bool | None]:
    completed = subprocess.run(
        ["bash", "-lc", command],
        cwd=execution_root,
        capture_output=True,
        text=True,
        check=False,
    )
    artifact_name = legacy_artifact_name or f"{hook_point}-{ordinal:03d}.yaml"
    artifact_path = task_dir(root, task) / "artifacts" / artifact_name
    artifact_payload = {
        "step": step,
        "hook_point": hook_point,
        "command": command,
        "blocking": blocking,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    artifact_content = yaml.safe_dump(artifact_payload, sort_keys=False)
    if len(artifact_content.encode("utf-8")) >= _COMPRESS_HOOK_ARTIFACT_MIN_BYTES:
        artifact_path = artifact_path.with_name(f"{artifact_path.name}.gz")
        _atomic_write_gzip_text(artifact_path, artifact_content)
    else:
        _atomic_write_text(artifact_path, artifact_content)
    artifact_label = artifact_path.relative_to(task_dir(root, task)).as_posix()
    status = "passed" if completed.returncode == 0 else "failed"
    append_journal(
        root,
        task,
        "\n".join(
            [
                f"Runner hook `{hook_point}` {status}: `{command}`.",
                f"- step: `{step}`",
                f"- blocking: `{blocking}`",
                f"- exit_code: `{completed.returncode}`",
                f"- artifact: `{artifact_label}`",
            ]
        ),
    )
    return {
        "point": hook_point,
        "command": command,
        "blocking": blocking,
        "exit_code": completed.returncode,
        "status": status,
        "artifact": artifact_label,
    }


def _runner_hook_warnings(hook_result: dict[str, str | int | bool | None]) -> list[str]:
    qualifier = "passed" if hook_result["status"] == "passed" else "failed"
    return [
        (
            f"runner hook {qualifier}: `{hook_result['point']}` "
            f"`{hook_result['command']}` (artifact: `{hook_result['artifact']}`)"
        )
    ]


def _runner_hook_feedback(hook_result: dict[str, str | int | bool | None]) -> str:
    return (
        f"Runner hook `{hook_result['point']}` `{hook_result['command']}` "
        f"{hook_result['status']} with exit code {hook_result['exit_code']} "
        f"(artifact: `{hook_result['artifact']}`)."
    )


def _flatten_runner_hook_warnings(
    hook_results: list[dict[str, str | int | bool | None]]
) -> list[str]:
    warnings: list[str] = []
    for hook_result in hook_results:
        warnings.extend(_runner_hook_warnings(hook_result))
    return warnings


def _flatten_runner_hook_feedback(
    hook_results: list[dict[str, str | int | bool | None]]
) -> list[str]:
    return [_runner_hook_feedback(hook_result) for hook_result in hook_results]


def _commit_to_git_report(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    *,
    auto_commit_enabled: bool,
    subagents: SubagentManager | None = None,
    config: LitehiveConfig | None = None,
) -> StageReport:
    if not auto_commit_enabled:
        task.status = "done"
        task.pipeline_status = "done"
        save_task(root, task)
        append_journal(root, task, "CommitToGit skipped: auto-commit disabled.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="pass",
            summary="CommitToGit skipped because auto-commit is disabled",
        )

    if not is_git_repo(root):
        task.status = "done"
        task.pipeline_status = "done"
        save_task(root, task)
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="pass",
            summary="CommitToGit skipped: not a git repository",
        )

    head_before = current_head(root)

    # Step 1: commit everything in the worktree
    if execution_root != root:
        subprocess.run(["git", "add", "-A"], cwd=execution_root, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"litehive: complete {task.id} {task.slug}"],
            cwd=execution_root, capture_output=True,
        )

    # Step 2: merge worktree into main
    merge_ok = False
    if execution_root != root:
        wt_head = current_head(execution_root)
        if wt_head:
            # Add and commit any dirty files on main so they don't block merge
            subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
            subprocess.run(["git", "commit", "-m", "chore: sync workspace state"],
                           cwd=root, capture_output=True)
            merge = subprocess.run(
                ["git", "merge", wt_head, "-m", f"litehive: complete {task.id} {task.slug}", "--no-edit"],
                cwd=root, capture_output=True, text=True,
            )
            if merge.returncode == 0:
                merge_ok = True
            else:
                # Merge failed - try agent resolution
                if subagents is not None:
                    conflict_proc = subprocess.run(
                        ["git", "diff", "--name-only", "--diff-filter=U"],
                        cwd=root, capture_output=True, text=True,
                    )
                    conflicts = [f.strip() for f in conflict_proc.stdout.splitlines() if f.strip()]
                    if conflicts:
                        append_journal(root, task,
                            f"Merge conflict on {len(conflicts)} file(s). Launching merge agent.")
                        engine_name = (config.recovery_engine if config and config.recovery_engine
                                       else task.engine or (config.default_engine if config else "codex"))
                        model = resolve_model(task, config, engine_name=engine_name) if config else None
                        subagents.run(
                            task, role="merge-resolver", engine_name=engine_name, model=model,
                            prompt=(
                                f"Git merge conflict. Conflicting files: {', '.join(conflicts)}\n"
                                f"Resolve the conflicts, git add the files, and git commit --no-edit.\n"
                            ),
                        )
                        # Check if agent resolved it
                        remaining = subprocess.run(
                            ["git", "diff", "--name-only", "--diff-filter=U"],
                            cwd=root, capture_output=True, text=True,
                        )
                        if not remaining.stdout.strip():
                            merge_ok = True
                if not merge_ok:
                    subprocess.run(["git", "merge", "--abort"], cwd=root, capture_output=True)
    else:
        merge_ok = True

    # Step 3: verify new commits landed on main
    head_after = current_head(root)
    if not merge_ok or head_after == head_before:
        # Merge failed or nothing changed - do NOT mark done, do NOT delete worktree
        append_journal(root, task, f"CommitToGit failed: merge did not produce new commits on main.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary="CommitToGit failed: merge did not produce new commits on main",
        )

    # Step 4: delete worktree (merge confirmed)
    if execution_root != root:
        worktree_path = get_task_worktree_path(task)
        if worktree_path:
            wt = (root / worktree_path).resolve()
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                           cwd=root, capture_output=True)
            task.git.worktree_path = None

    task.status = "done"
    task.pipeline_status = "done"
    set_task_commit_sha(task, head_after)
    save_task(root, task)
    append_journal(root, task, f"CommitToGit complete. Commit: {head_after}")

    # Push to remote
    push = subprocess.run(["git", "push"], cwd=root, capture_output=True, text=True)
    if push.returncode != 0:
        append_journal(root, task, f"Push failed: {push.stderr.strip()}")

    return StageReport(
        task_id=task.id,
        step="commit_to_git",
        verdict="pass",
        summary=f"CommitToGit complete. Commit: {head_after[:8]}",
    )
