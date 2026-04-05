"""High-level runtime flow for executing queued tasks."""

from __future__ import annotations

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
    checkpoint_message,
    commit_task,
    current_head,
    find_commit_by_subject,
    has_changes,
    is_git_repo,
    rebase_worktree_onto,
    remove_worktree,
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
        recovered_summary = _recover_existing_integrated_checkpoint(root, task)
        if recovered_summary is not None:
            return recovered_summary
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
    interruption = task.runtime.interruption
    if interruption is not None and interruption.reason == "Task stopped via CLI":
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
            raise GitError(f"task worktree is missing: {worktree_path_value}")
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


def _cleanup_task_worktree(root: Path, task: TaskRecord) -> None:
    worktree_path_value = get_task_worktree_path(task)
    if not worktree_path_value:
        return
    worktree_path = (root / worktree_path_value).resolve()
    if worktree_path.exists():
        remove_worktree(root, worktree_path, force=True)
    clear_task_worktree_path(task)
    save_task(root, task)


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


def _self_heal_worktree_path(source_root: Path, task: TaskRecord, fingerprint: str) -> Path:
    return source_root / ".litehive" / "worktrees" / f"self-heal-{task.id}-{fingerprint}"


def _run_repo_pytest(repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "pytest"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


def _append_self_heal_thread_comment(
    root: Path,
    task: TaskRecord,
    *,
    step: str,
    headline: str,
    fingerprint: str,
    source_root: Path | None = None,
    worktree_path: Path | None = None,
    test_command: str | None = None,
    test_result: str | None = None,
    merge_outcome: str,
    requeue_decision: str,
    blocker: str | None = None,
) -> None:
    fields = [
        headline,
        "classification: litehive_bug",
        f"traceback_fingerprint: {fingerprint}",
        f"litehive_source_path: {source_root if source_root is not None else 'unavailable'}",
        f"litehive_worktree: {worktree_path if worktree_path is not None else 'not-created'}",
    ]
    if test_command is not None:
        fields.append(f"test_command: {test_command}")
    if test_result is not None:
        fields.append(f"test_result: {test_result}")
    fields.append(f"merge_outcome: {merge_outcome}")
    fields.append(f"requeue_decision: {requeue_decision}")
    if blocker:
        fields.append(f"blocker: {blocker}")
    append_thread_comment(
        root,
        task,
        TaskThreadComment(
            role="recovery",
            step=step,
            verdict="comment",
            message="\n".join(fields),
        ),
    )


def _attempt_litehive_self_heal(
    root: Path,
    task: TaskRecord,
    step: str,
    failed_report: StageReport,
    *,
    subagents: SubagentManager,
    config: LitehiveConfig,
    engine_name: str,
    model_name: str | None,
    traceback_text: str,
    source_root: Path | None,
) -> StageReport:
    fingerprint = _traceback_fingerprint(traceback_text, failed_report.summary)
    if fingerprint in task.runtime.self_heal_traceback_fingerprints:
        summary = (
            "Litehive self-heal already ran once for this traceback fingerprint; leaving the task blocked to avoid a recovery loop."
        )
        record_recovery_report(
            root,
            task,
            trigger="stage_failure",
            stage=step,
            summary=summary,
            runnable_state="blocked",
            failure_classification="litehive_bug",
            blocker=summary,
            actions=[
                RecoveryAction(
                    action="self_heal_skip_repeat",
                    applied=False,
                    summary="Skipped repeated Litehive self-heal attempt for an already-seen traceback fingerprint.",
                    metadata={"traceback_fingerprint": fingerprint},
                )
            ],
            warnings=[failed_report.summary],
        )
        _append_self_heal_thread_comment(
            root,
            task,
            step=step,
            headline="Litehive self-heal skipped.",
            fingerprint=fingerprint,
            source_root=source_root,
            merge_outcome="not-attempted",
            requeue_decision="blocked same-stage retry to avoid loop",
            blocker=summary,
        )
        return StageReport(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            verdict="blocked",
            summary=summary,
            failure_classification="litehive_bug",
            failure_diagnostics={"traceback_fingerprint": fingerprint},
        )

    if source_root is None or not source_root.exists() or not is_git_repo(source_root):
        blocker = (
            "Litehive bug detected from traceback, but `litehive_source_path` is missing or is not a git repository."
        )
        record_recovery_report(
            root,
            task,
            trigger="stage_failure",
            stage=step,
            summary="Litehive self-heal is blocked because the configured source repo is unavailable.",
            runnable_state="blocked",
            failure_classification="litehive_bug",
            blocker=blocker,
            actions=[
                RecoveryAction(
                    action="self_heal_blocked",
                    applied=False,
                    summary="Could not launch Litehive self-heal because `litehive_source_path` did not resolve to a usable git repo.",
                    metadata={
                        "configured_litehive_source_path": config.litehive_source_path,
                        "traceback_fingerprint": fingerprint,
                    },
                )
            ],
            warnings=[failed_report.summary],
        )
        _append_self_heal_thread_comment(
            root,
            task,
            step=step,
            headline="Litehive self-heal blocked before launch.",
            fingerprint=fingerprint,
            source_root=source_root,
            merge_outcome="not-attempted",
            requeue_decision="blocked awaiting usable litehive_source_path",
            blocker=blocker,
        )
        return StageReport(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            verdict="blocked",
            summary=blocker,
            failure_classification="litehive_bug",
            failure_diagnostics={"traceback_fingerprint": fingerprint},
        )

    task.runtime.self_heal_traceback_fingerprints.append(fingerprint)
    save_task_runtime(root, task)

    worktree_path = _self_heal_worktree_path(source_root, task, fingerprint)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        shutil.rmtree(worktree_path)
    add_worktree(source_root, worktree_path, ref=current_head(source_root) or "HEAD")
    _append_self_heal_thread_comment(
        root,
        task,
        step=step,
        headline="Litehive self-heal launched.",
        fingerprint=fingerprint,
        source_root=source_root,
        worktree_path=worktree_path,
        merge_outcome="pending",
        requeue_decision=f"pending same-stage `{step}` if self-heal succeeds",
    )
    append_journal(
        root,
        task,
        (
            "Traceback classified as Litehive-owned. Launching self-heal in the Litehive repo.\n"
            f"- litehive_source_path: `{source_root}`\n"
            f"- litehive_worktree: `{worktree_path}`\n"
            f"- traceback_fingerprint: `{fingerprint}`"
        ),
    )
    try:
        recovery_manager = SubagentManager(root, execution_root=worktree_path)
        prompt = (
            f"You are repairing a Litehive bug that interrupted external-project task {task.id} ({task.title}).\n\n"
            f"Do all code changes inside the Litehive worktree at: {worktree_path}\n"
            f"Do not edit the external project at: {root}\n\n"
            f"Current stage to unblock: {step}\n"
            f"Failure summary: {failed_report.summary}\n"
            f"Traceback:\n{traceback_text}\n\n"
            "Requirements:\n"
            "- fix the Litehive bug in this Litehive worktree\n"
            "- keep the fix scoped to the failure above\n"
            "- do not merge anything yourself; the wrapper will run `uv run pytest` and merge if the worktree is good\n"
            "- submit a detailed `litehive report` explaining the fix and evidence\n"
        )
        recovery_result = recovery_manager.run(
            task,
            role="recovery",
            engine_name=engine_name,
            prompt=prompt,
            model=model_name,
        )
        pytest_result = _run_repo_pytest(worktree_path)
        pytest_command = "uv run pytest"
        pytest_summary = f"{pytest_command} exited {pytest_result.returncode}"
        if pytest_result.returncode != 0:
            summary = "Litehive self-heal produced changes, but `uv run pytest` failed so the fix was not merged."
            record_recovery_report(
                root,
                task,
                trigger="stage_failure",
                stage=step,
                summary=summary,
                runnable_state="blocked",
                failure_classification="litehive_bug",
                blocker=pytest_summary,
                actions=[
                    RecoveryAction(
                        action="self_heal_attempt",
                        summary="Ran Litehive self-heal in the configured Litehive repo.",
                        metadata={
                            "traceback_fingerprint": fingerprint,
                            "litehive_worktree": str(worktree_path),
                        },
                    ),
                    RecoveryAction(
                        action="run_pytest",
                        applied=False,
                        summary="Litehive test gate failed; merge was skipped.",
                        metadata={
                            "command": pytest_command,
                            "exit_code": pytest_result.returncode,
                        },
                    ),
                ],
                warnings=[pytest_result.stderr.strip() or pytest_result.stdout.strip()],
                recovery_subagent_id=recovery_result.ref.id,
                recovery_subagent_path=recovery_result.ref.path,
            )
            _append_self_heal_thread_comment(
                root,
                task,
                step=step,
                headline="Litehive self-heal blocked after test gate.",
                fingerprint=fingerprint,
                source_root=source_root,
                worktree_path=worktree_path,
                test_command=pytest_command,
                test_result=f"fail (exit {pytest_result.returncode})",
                merge_outcome="skipped because pytest failed",
                requeue_decision="blocked until Litehive tests pass",
                blocker=pytest_summary,
            )
            return StageReport(
                task_id=task.id,
                step=step,  # type: ignore[arg-type]
                verdict="blocked",
                summary=summary,
                failure_classification="litehive_bug",
                failure_diagnostics={
                    "traceback_fingerprint": fingerprint,
                    "pytest_exit_code": pytest_result.returncode,
                },
                warnings=[pytest_summary],
            )

        _pull_rebase_main(source_root, subagents=subagents, task=task, config=config)
        commit_message = f"litehive: self-heal {task.id} {step} {fingerprint}"
        healed_commit = _commit_all_in_worktree(worktree_path, commit_message)
        if healed_commit is None:
            summary = "Litehive self-heal did not produce a committable fix."
            record_recovery_report(
                root,
                task,
                trigger="stage_failure",
                stage=step,
                summary=summary,
                runnable_state="blocked",
                failure_classification="litehive_bug",
                blocker=summary,
                actions=[
                    RecoveryAction(
                        action="self_heal_attempt",
                        summary="Litehive recovery agent ran but left no committable changes.",
                        metadata={"traceback_fingerprint": fingerprint, "litehive_worktree": str(worktree_path)},
                    ),
                    RecoveryAction(
                        action="run_pytest",
                        summary="Litehive test gate passed before merge evaluation.",
                        metadata={"command": pytest_command, "exit_code": 0},
                    ),
                ],
                recovery_subagent_id=recovery_result.ref.id,
                recovery_subagent_path=recovery_result.ref.path,
            )
            _append_self_heal_thread_comment(
                root,
                task,
                step=step,
                headline="Litehive self-heal blocked after no committable fix.",
                fingerprint=fingerprint,
                source_root=source_root,
                worktree_path=worktree_path,
                test_command=pytest_command,
                test_result="pass",
                merge_outcome="skipped because no committable changes were produced",
                requeue_decision="blocked pending manual recovery",
                blocker=summary,
            )
            return StageReport(
                task_id=task.id,
                step=step,  # type: ignore[arg-type]
                verdict="blocked",
                summary=summary,
                failure_classification="litehive_bug",
                failure_diagnostics={"traceback_fingerprint": fingerprint},
            )

        merged_sha = _merge_worktree_into_main(
            source_root,
            worktree_path,
            commit_message,
            subagents=subagents,
            task=task,
            config=config,
        )
        task.status = "queued"
        task.pipeline_status = step  # type: ignore[assignment]
        clear_task_outcome(root, task)
        summary = f"Litehive self-heal merged to Litehive main and requeued the task at `{step}`."
        actions = [
            RecoveryAction(
                action="self_heal_attempt",
                summary="Ran Litehive self-heal in a Litehive-owned worktree.",
                metadata={"traceback_fingerprint": fingerprint, "litehive_worktree": str(worktree_path)},
            ),
            RecoveryAction(
                action="run_pytest",
                summary="Litehive test gate passed before merge.",
                metadata={"command": pytest_command, "exit_code": 0},
            ),
            RecoveryAction(
                action="merge_litehive_main",
                summary="Merged the Litehive self-heal fix onto Litehive main.",
                metadata={"commit_sha": merged_sha, "source_root": str(source_root)},
            ),
            RecoveryAction(
                action="requeue_stage",
                summary="Requeued the originating task at the same stage for the next wrapper iteration.",
                metadata={"stage": step, "decision": "requeue"},
            ),
        ]
        record_recovery_report(
            root,
            task,
            trigger="stage_failure",
            stage=step,
            summary=summary,
            runnable_state="runnable",
            failure_classification="litehive_bug",
            actions=actions,
            recovery_subagent_id=recovery_result.ref.id,
            recovery_subagent_path=recovery_result.ref.path,
        )
        append_thread_comment(
            root,
            task,
            TaskThreadComment(
                role="recovery",
                step=step,
                verdict="comment",
                message=(
                    "Litehive self-heal completed.\n"
                    "classification: litehive_bug\n"
                    f"traceback_fingerprint: {fingerprint}\n"
                    f"litehive_source_path: {source_root}\n"
                    f"litehive_worktree: {worktree_path}\n"
                    f"test_command: {pytest_command}\n"
                    "test_result: pass\n"
                    f"merge_outcome: merged to Litehive main at {merged_sha}\n"
                    f"requeue_decision: same-stage `{step}`"
                ),
            ),
        )
        return StageReport(
            task_id=task.id,
            step=step,  # type: ignore[arg-type]
            verdict="pass",
            summary=summary,
            failure_classification="litehive_bug",
            failure_diagnostics={
                "traceback_fingerprint": fingerprint,
                "litehive_merge_commit": merged_sha,
            },
            retry_decision="retry",
            warnings=["self-heal applied in Litehive main; rerun required to pick up the fix"],
        )
    finally:
        if worktree_path.exists():
            remove_worktree(source_root, worktree_path, force=True)


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
    if failure_owner == "litehive" and config is not None and (source_root is None or source_root != root.resolve()):
        return _attempt_litehive_self_heal(
            root,
            task,
            step,
            failed_report,
            subagents=subagents,
            config=config,
            engine_name=engine_name,
            model_name=model_name,
            traceback_text=traceback_text,
            source_root=source_root,
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


def _attempt_commit_recovery(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    error_message: str,
    *,
    subagents: SubagentManager | None = None,
    config: LitehiveConfig | None = None,
) -> str | None:
    """Launch a recovery agent to fix a commit_to_git failure. Returns integrated SHA or None."""
    if subagents is None:
        return None

    evidence = collect_recovery_evidence(root, task, stage="commit_to_git")
    evidence_lines = "\n".join(
        f"- {item.label}: {item.path or 'n/a'} ({'present' if item.exists else 'missing'}) :: {item.summary}"
        for item in evidence
    )
    prompt = (
        f"You are running as Litehive's recovery agent for task {task.id} ({task.title}).\n\n"
        f"Trigger: commit_to_git failed.\n"
        f"Error: {error_message}\n\n"
        f"Working directory: {execution_root}\n"
        f"Main repo: {root}\n\n"
        f"Recovery evidence gathered automatically:\n{evidence_lines}\n\n"
        f"Your job is to diagnose and fix whatever is preventing the task's changes "
        f"from being committed and merged into main. Common issues:\n"
        f"- Merge conflicts: resolve them and run `git add <file> && git commit --no-edit`\n"
        f"- Dirty state: stage and commit changes with `git add -A && git commit -m 'fix'`\n"
        f"- Worktree issues: check `git status` in both {execution_root} and {root}\n\n"
        f"After fixing, ensure the worktree changes are merged into main:\n"
        f"1. In the worktree: `git add -A && git commit -m '{checkpoint_message(task)}'`\n"
        f"2. In main repo: `cd {root} && git merge <worktree-HEAD>`\n\n"
        f"If you cannot fix it, explain why in your report.\n"
        f"Submit your result: litehive report --verdict pass --role recovery --step commit_to_git "
        f"--message '<what you fixed>'\n"
    )

    engine_name, model_name = _resolve_recovery_engine(task, config)
    recovery_result = subagents.run(
        task,
        role="recovery",
        engine_name=engine_name,
        prompt=prompt,
        model=model_name,
    )

    # Check if the recovery agent succeeded — is main HEAD ahead of where it was?
    head = current_head(root)
    if head is None:
        return None

    # Check if merge completed (no conflicts left)
    conflict_files = _list_conflict_files(root)
    if conflict_files:
        subprocess.run(["git", "merge", "--abort"], cwd=root, capture_output=True)
        record_recovery_report(
            root,
            task,
            trigger="commit_to_git_failure",
            stage="commit_to_git",
            summary="Recovery agent could not safely complete commit_to_git.",
            runnable_state="blocked",
            failure_classification="commit_to_git_failure",
            blocker="merge conflicts remained after recovery attempt",
            actions=[
                RecoveryAction(
                    action="recover_commit_to_git",
                    applied=False,
                    summary="Recovery agent attempted commit recovery but unresolved conflicts remained.",
                    metadata={"conflict_files": conflict_files},
                )
            ],
            warnings=[error_message],
            recovery_subagent_id=recovery_result.ref.id,
            recovery_subagent_path=recovery_result.ref.path,
        )
        return None

    record_recovery_report(
        root,
        task,
        trigger="commit_to_git_failure",
        stage="commit_to_git",
        summary="Recovery agent completed bounded commit_to_git repair.",
        runnable_state="runnable",
        failure_classification="commit_to_git_failure",
        actions=[
            RecoveryAction(
                action="recover_commit_to_git",
                summary="Recovery agent repaired the failed commit_to_git flow and produced an integrated commit.",
                metadata={"commit_sha": head},
            )
        ],
        warnings=[error_message],
        recovery_subagent_id=recovery_result.ref.id,
        recovery_subagent_path=recovery_result.ref.path,
    )
    return head


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


def _pull_rebase_main(
    root: Path,
    *,
    subagents: SubagentManager | None = None,
    task: TaskRecord | None = None,
    config: LitehiveConfig | None = None,
) -> None:
    """Pull latest from remote with rebase. If rebase conflicts, launch merge agent."""
    proc = subprocess.run(
        ["git", "pull", "--rebase"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return

    # Rebase failed — check if there are conflicts to resolve
    conflict_files = _list_conflict_files(root)
    if not conflict_files:
        # No conflicts but pull failed (no remote, no tracking branch, etc.) — skip silently
        subprocess.run(["git", "rebase", "--abort"], cwd=root, capture_output=True)
        return

    # Launch merge agent to resolve rebase conflicts
    if subagents is not None and task is not None:
        append_journal(
            root, task,
            f"git pull --rebase conflicted on {len(conflict_files)} file(s). Launching merge agent.",
        )
        prompt = (
            f"A `git pull --rebase` caused conflicts in this repository.\n"
            f"Conflicting files: {', '.join(conflict_files)}\n\n"
            f"Resolve the conflicts, then:\n"
            f"1. Edit each file to remove conflict markers\n"
            f"2. `git add <file>` for each resolved file\n"
            f"3. `git rebase --continue`\n"
        )
        engine_name, model_name = _resolve_recovery_engine(task, config)
        subagents.run(task, role="merge-resolver", engine_name=engine_name, prompt=prompt, model=model_name)

        remaining = _list_conflict_files(root)
        if remaining:
            subprocess.run(["git", "rebase", "--abort"], cwd=root, capture_output=True)
    else:
        subprocess.run(["git", "rebase", "--abort"], cwd=root, capture_output=True)


def _commit_all_in_worktree(execution_root: Path, message: str) -> str | None:
    """Stage everything and commit in the worktree. Returns commit SHA or None."""
    subprocess.run(
        ["git", "add", "-A"],
        cwd=execution_root,
        capture_output=True,
        text=True,
    )
    proc = subprocess.run(
        ["git", "commit", "-m", message, "--allow-empty"],
        cwd=execution_root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return current_head(execution_root)


def _merge_worktree_into_main(
    root: Path,
    execution_root: Path,
    message: str,
    *,
    subagents: SubagentManager | None = None,
    task: TaskRecord | None = None,
    config: LitehiveConfig | None = None,
) -> str:
    """Merge the worktree HEAD into main. On conflict, run an agent to resolve."""
    wt_head = current_head(execution_root)
    if wt_head is None:
        raise GitError("worktree HEAD could not be resolved")
    proc = subprocess.run(
        ["git", "merge", wt_head, "-m", message, "--no-edit"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        head = current_head(root)
        if head is None:
            raise GitError("merge completed but HEAD could not be resolved")
        return head

    # Merge failed — try agent-assisted resolution
    if subagents is None or task is None:
        subprocess.run(["git", "merge", "--abort"], cwd=root, capture_output=True)
        raise GitError(
            f"merge of worktree into main failed with conflicts: {proc.stderr.strip()}"
        )

    # Leave conflict markers in place and let an agent resolve them
    conflict_files = _list_conflict_files(root)
    if not conflict_files:
        subprocess.run(["git", "merge", "--abort"], cwd=root, capture_output=True)
        raise GitError(f"merge failed but no conflict files found: {proc.stderr.strip()}")

    append_journal(
        root,
        task,
        f"CommitToGit merge conflict on {len(conflict_files)} file(s): {', '.join(conflict_files)}. "
        "Launching merge resolution agent.",
    )

    prompt = (
        f"There is a git merge conflict in this repository.\n"
        f"Conflicting files: {', '.join(conflict_files)}\n\n"
        f"The merge is in progress. The conflict markers (<<<<<<< ======= >>>>>>>) "
        f"are in the files listed above.\n\n"
        f"Task context: {task.title}\n"
        f"Goal: {task.goal or 'not specified'}\n\n"
        f"Instructions:\n"
        f"1. Read each conflicting file\n"
        f"2. Resolve the conflicts by keeping the correct code (prefer the incoming changes "
        f"from the task worktree unless they break existing functionality)\n"
        f"3. Remove all conflict markers\n"
        f"4. Run: git add <resolved-file> for each file\n"
        f"5. Run: git commit --no-edit to complete the merge\n"
        f"6. Do NOT modify any files beyond resolving the conflicts\n"
    )

    engine_name, model_name = _resolve_recovery_engine(task, config)
    result = subagents.run(
        task,
        role="merge-resolver",
        engine_name=engine_name,
        prompt=prompt,
        model=model_name,
    )

    # Check if the agent resolved the merge
    remaining_conflicts = _list_conflict_files(root)
    if remaining_conflicts:
        subprocess.run(["git", "merge", "--abort"], cwd=root, capture_output=True)
        raise GitError(
            f"merge agent failed to resolve conflicts in: {', '.join(remaining_conflicts)}"
        )

    head = current_head(root)
    if head is None:
        raise GitError("merge completed but HEAD could not be resolved")

    append_journal(root, task, "Merge conflicts resolved by agent.")
    return head


def _list_conflict_files(root: Path) -> list[str]:
    """List files with unresolved merge conflicts."""
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [f.strip() for f in proc.stdout.splitlines() if f.strip()]


def run_late_stage_completion_preflight(
    root: Path,
    task: TaskRecord,
) -> dict[str, object]:
    root = root.resolve()
    diagnostics: dict[str, str | int | bool | None | list[str]] = {
        "phase": "late_stage_preflight",
        "workspace_root": str(root),
        "auto_commit_enabled": task.git.auto_commit,
        "pipeline_status": task.pipeline_status,
        "checkpoint_attempt": task.git.checkpoint_attempts,
        "planned_checkpoint_attempt": task.git.checkpoint_attempts + 1,
    }

    def result(
        *,
        passed: bool,
        summary: str,
        classification: str | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, object]:
        hook_result: dict[str, str | int | bool | None] = {
            "point": "before_commit_to_git_preflight",
            "status": "passed" if passed else "failed",
            "summary": summary,
        }
        if classification is not None:
            hook_result["classification"] = classification
        return {
            "passed": passed,
            "summary": summary,
            "classification": classification,
            "warnings": warnings or [],
            "diagnostics": dict(diagnostics),
            "hook_result": hook_result,
        }

    if not task.git.auto_commit:
        diagnostics["check"] = "auto_commit_disabled"
        return result(
            passed=True,
            summary="Late-stage commit/worktree preflight skipped because auto-commit is disabled",
        )

    message = checkpoint_message(task, attempt=task.git.checkpoint_attempts + 1)
    diagnostics["planned_commit_message"] = message
    diagnostics["recorded_worktree_path"] = get_task_worktree_path(task)

    if not is_git_repo(root):
        diagnostics["check"] = "git_repo"
        return result(
            passed=False,
            summary="workspace is not a git repository",
            classification="not_git_repo",
        )

    if task.git.commit_sha is not None:
        # Clear stale commit SHA from a previous attempt so commit_to_git can proceed
        diagnostics["check"] = "commit_sha"
        diagnostics["cleared_stale_commit_sha"] = task.git.commit_sha
        task.git.commit_sha = None
        from litehive.tasks import save_task
        save_task(root, task)

    worktree_path_value = get_task_worktree_path(task)
    if not worktree_path_value:
        diagnostics["check"] = "task_worktree"
        return result(
            passed=False,
            summary="task worktree path is missing before commit_to_git",
            classification="missing_task_worktree",
        )

    worktree_path = (root / worktree_path_value).resolve()
    diagnostics["worktree_path"] = str(worktree_path)
    if not worktree_path.exists():
        diagnostics["check"] = "task_worktree"
        return result(
            passed=False,
            summary=f"task worktree is missing: {worktree_path_value}",
            classification="missing_task_worktree",
        )

    worktree_list = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if worktree_list.returncode != 0:
        diagnostics["check"] = "worktree_registry"
        diagnostics["error"] = worktree_list.stderr.strip() or "git worktree list failed"
        return result(
            passed=False,
            summary=diagnostics["error"],
            classification="worktree_registry_check_failed",
            warnings=[str(diagnostics["error"])],
        )

    registered_worktrees = {
        line.removeprefix("worktree ").strip()
        for line in worktree_list.stdout.splitlines()
        if line.startswith("worktree ")
    }
    diagnostics["registered_worktree"] = str(worktree_path) in registered_worktrees
    if str(worktree_path) not in registered_worktrees:
        diagnostics["check"] = "worktree_registry"
        return result(
            passed=False,
            summary=f"task worktree is not registered with git: {worktree_path_value}",
            classification="invalid_task_worktree",
        )

    if not is_git_repo(worktree_path):
        diagnostics["check"] = "worktree_git_dir"
        return result(
            passed=False,
            summary=f"task worktree is not a git checkout: {worktree_path_value}",
            classification="invalid_task_worktree",
        )

    main_head = current_head(root)
    worktree_head = current_head(worktree_path)
    diagnostics["main_head"] = main_head
    diagnostics["worktree_head"] = worktree_head
    if worktree_head is None:
        diagnostics["check"] = "worktree_head"
        return result(
            passed=False,
            summary=f"task worktree HEAD could not be resolved: {worktree_path_value}",
            classification="worktree_head_unresolved",
        )
    if main_head is not None and worktree_head != main_head:
        # Worktree HEAD differs from main - this is normal when the agent committed work.
        # commit_to_git will merge it.
        diagnostics["check"] = "worktree_head_match"
        diagnostics["note"] = "worktree has commits ahead of main - will be merged during commit_to_git"

    root_conflicts = _list_conflict_files(root)
    worktree_conflicts = _list_conflict_files(worktree_path)
    diagnostics["root_conflicts"] = root_conflicts
    diagnostics["worktree_conflicts"] = worktree_conflicts
    if root_conflicts or worktree_conflicts:
        # Don't block - the merge agent will resolve conflicts during commit_to_git
        diagnostics["check"] = "merge_conflicts"
        diagnostics["note"] = "conflicts detected but will be resolved by merge agent during commit"

    diagnostics["check"] = "complete"
    return result(
        passed=True,
        summary="Late-stage commit/worktree preflight passed",
    )


def _recover_or_validate_clean_task_worktree(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
) -> str | None:
    """If the worktree has changes vs main, commit them and merge into main."""
    if execution_root == root:
        return None

    wt_head = current_head(execution_root)
    main_head = current_head(root)
    if wt_head is None or wt_head == main_head:
        return None

    message = checkpoint_message(task, attempt=task.git.checkpoint_attempts + 1)
    return _merge_worktree_into_main(root, execution_root, message)


def _reconcile_existing_checkpoint_commit(root: Path, task: TaskRecord) -> str | None:
    if task.git.commit_sha is not None or task.git.checkpoint_attempts < 1:
        return None
    existing_integrated_sha = _find_existing_checkpoint_commit(root, task)
    if existing_integrated_sha is None:
        return None
    _finalize_recovered_commit_task(task, commit_sha=existing_integrated_sha)
    return existing_integrated_sha


def _recover_existing_integrated_checkpoint(root: Path, task: TaskRecord) -> ExecutionSummary | None:
    if (
        task.status != "done"
        or task.pipeline_status != "done"
    ):
        return None
    existing_integrated_sha = _reconcile_existing_checkpoint_commit(root, task)
    if existing_integrated_sha is None:
        return None

    state = load_state(root)
    if state.active_task_id == task.id:
        state.active_task_id = None
    state.queue = [queued_id for queued_id in state.queue if queued_id != task.id]
    journal_message = _finalize_recovered_commit_task(task, commit_sha=existing_integrated_sha)
    persist_task_and_state(
        root,
        task=task,
        state=state,
        journal_message=journal_message,
    )
    _cleanup_task_worktree(root, task)
    save_task_runtime(root, task)
    return ExecutionSummary(
        task=task,
        result=RunResult(final_status="done", steps_executed=0, last_verdict="pass"),
        commit_sha=task.git.commit_sha,
    )


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
                report.feedback = "\n\n".join([*execution_events, report.feedback]).strip()
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
                report.feedback = "\n\n".join([*execution_events, result.transcript]).strip()
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
    report.feedback = "\n\n".join(
        [
            *_flatten_runner_hook_feedback(hook_results),
            report.feedback,
        ]
    ).strip()


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
    def failure_report(
        *,
        classification: str,
        summary: str,
        error_text: str | None = None,
        phase: str,
        base_sha: str | None = None,
        message: str | None = None,
        dirty_entries: list[str] | None = None,
        attempt: int | None = None,
    ) -> StageReport:
        diagnostics = _commit_to_git_failure_diagnostics(
            root,
            execution_root,
            task,
            phase=phase,
            error_text=error_text,
            base_sha=base_sha,
            message=message,
            dirty_entries=dirty_entries,
            attempt=attempt,
        )
        append_journal(root, task, summary)
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary=summary,
            warnings=[warning for warning in [error_text] if warning],
            failure_classification=classification,
            failure_diagnostics=diagnostics,
        )

    if not auto_commit_enabled:
        task.status = "done"
        task.pipeline_status = "done"
        _cleanup_task_worktree(root, task)
        append_journal(root, task, "CommitToGit skipped: auto-commit disabled.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="pass",
            summary="CommitToGit skipped because auto-commit is disabled",
            warnings=["auto-commit disabled"],
        )

    if not is_git_repo(root):
        return failure_report(
            classification="not_git_repo",
            summary="CommitToGit failed: workspace is not a git repository",
            error_text="workspace is not a git repository",
            phase="preflight",
        )

    reconciled_sha = _reconcile_existing_checkpoint_commit(root, task)
    if reconciled_sha is not None:
        _cleanup_task_worktree(root, task)
        save_task(root, task)
        append_journal(
            root,
            task,
            "CommitToGit reconciled an existing Litehive checkpoint commit.",
        )
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="pass",
            summary="CommitToGit reconciled an existing checkpoint commit",
            files_changed=[],
        )

    try:
        dirty_entries = status_porcelain(execution_root)
    except GitError as exc:
        return failure_report(
            classification="status_failed",
            summary=f"CommitToGit failed: {exc}",
            error_text=str(exc),
            phase="status",
        )

    if not dirty_entries:
        try:
            recovered_sha = _recover_or_validate_clean_task_worktree(root, execution_root, task)
        except GitError as exc:
            return failure_report(
                classification="clean_worktree_recovery_failed",
                summary=f"CommitToGit failed: {exc}",
                error_text=str(exc),
                phase="clean_worktree_recovery",
            )

        if recovered_sha is not None:
            set_task_commit_sha(task, recovered_sha)
            task.status = "done"
            task.pipeline_status = "done"
            _cleanup_task_worktree(root, task)
            save_task(root, task)
            append_journal(
                root,
                task,
                "CommitToGit recovered and integrated an existing Litehive checkpoint from the task worktree.",
            )
            return StageReport(
                task_id=task.id,
                step="commit_to_git",
                verdict="pass",
                summary="CommitToGit recovered and integrated an existing task worktree checkpoint",
                files_changed=[],
            )

        head_sha = current_head(root)
        set_task_commit_sha(task, head_sha)
        task.status = "done"
        task.pipeline_status = "done"
        _cleanup_task_worktree(root, task)
        save_task(root, task)
        append_journal(
            root,
            task,
            "CommitToGit skipped: task worktree was already clean and no task-local changes remained.",
        )
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="pass",
            summary="CommitToGit skipped because task worktree was already clean",
            warnings=["no changes to commit"],
            files_changed=[],
        )

    # If all dirty entries are transient .litehive/ runtime files, skip commit.
    # Config files (.gitignore, config.yaml, context.md, state.yaml) ARE committable.
    _LITEHIVE_TRACKED_FILES = {
        ".litehive/.gitignore",
        ".litehive/config.yaml",
        ".litehive/context.md",
        ".litehive/state.yaml",
    }
    code_dirty = [
        e for e in dirty_entries
        if not (_status_entry_path(e) or "").startswith(".litehive/")
        or (_status_entry_path(e) or "") in _LITEHIVE_TRACKED_FILES
    ]
    if not code_dirty:
        head_sha = current_head(root)
        set_task_commit_sha(task, head_sha)
        task.status = "done"
        task.pipeline_status = "done"
        _cleanup_task_worktree(root, task)
        save_task(root, task)
        append_journal(root, task, "CommitToGit skipped: only workspace metadata changed.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="pass",
            summary="CommitToGit skipped because only workspace metadata changed",
            warnings=["no code changes to commit"],
            files_changed=[],
        )

    try:
        base_sha = current_head(root)
        attempt = task.git.checkpoint_attempts + 1
        message = checkpoint_message(task, attempt=attempt)
        previous_base_sha = task.git.checkpoint_base_sha
        previous_attempts = task.git.checkpoint_attempts
        previous_rollback_attempt = task.git.rolled_back_checkpoint_attempt
        previous_status = task.status
        previous_pipeline_status = task.pipeline_status
        state = load_state(root)
        set_task_commit_sha(task, None)
        task.git.checkpoint_base_sha = base_sha
        task.git.checkpoint_attempts = attempt
        task.git.rolled_back_checkpoint_attempt = None
        task.status = "done"
        task.pipeline_status = "done"
        if state.active_task_id == task.id:
            state.active_task_id = None
        state.queue = [queued_id for queued_id in state.queue if queued_id != task.id]
        append_journal(
            root,
            task,
            (
                "CommitToGit requested.\n"
                f"- base: `{base_sha or 'initial commit'}`\n"
                f"- message: `{message}`\n"
                f"- worktree: `{get_task_worktree_path(task) or execution_root}`"
            ),
        )
        persist_task_and_state(root, task=task, state=state)
        # Pull latest from remote before merging to avoid push conflicts
        _pull_rebase_main(root, subagents=subagents, task=task, config=config)
        if execution_root != root:
            _commit_all_in_worktree(execution_root, message)
            integrated_sha = _merge_worktree_into_main(
                root, execution_root, message,
                subagents=subagents, task=task, config=config,
            )
        else:
            checkpoint = commit_task(root, message, paths=None)
            if checkpoint is None:
                raise GitError("git commit prerequisites were not met")
            integrated_sha = checkpoint.commit_sha
    except Exception as exc:
        # Try recovery agent before giving up — catch ALL errors, not just GitError
        if subagents is not None and task is not None:
            append_journal(root, task, f"CommitToGit failed: {exc}. Launching recovery agent.")
            recovery_sha = _attempt_commit_recovery(
                root, execution_root, task, str(exc),
                subagents=subagents, config=config,
            )
            if recovery_sha is not None:
                set_task_commit_sha(task, recovery_sha)
                _cleanup_task_worktree(root, task)
                save_task(root, task)
                save_task_runtime(root, task)
                append_journal(root, task, "CommitToGit recovered by agent.")
                return StageReport(
                    task_id=task.id,
                    step="commit_to_git",
                    verdict="pass",
                    summary="CommitToGit recovered by agent after initial failure",
                )

        task.git.checkpoint_base_sha = previous_base_sha
        task.git.checkpoint_attempts = previous_attempts
        task.git.rolled_back_checkpoint_attempt = previous_rollback_attempt
        set_task_commit_sha(task, None)
        task.status = previous_status
        task.pipeline_status = previous_pipeline_status
        state = load_state(root)
        state.queue = [queued_id for queued_id in state.queue if queued_id != task.id]
        persist_task_and_state(root, task=task, state=state)
        return failure_report(
            classification="checkpoint_failed",
            summary=f"CommitToGit failed: {exc}",
            error_text=str(exc),
            phase="checkpoint",
            base_sha=base_sha,
            message=message,
            dirty_entries=dirty_entries,
            attempt=attempt,
        )

    set_task_commit_sha(task, integrated_sha)
    _cleanup_task_worktree(root, task)
    save_task(root, task)
    try:
        save_task_runtime(root, task)
    except Exception as runtime_exc:
        append_journal(root, task, f"Warning: save_task_runtime failed: {runtime_exc}")

    # Push to remote if one is configured
    push_result = subprocess.run(
        ["git", "push"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    push_warning: list[str] = []
    if push_result.returncode != 0:
        push_warning = [f"git push failed: {push_result.stderr.strip()}"]
        append_journal(root, task, f"CommitToGit push failed: {push_result.stderr.strip()}")
    else:
        append_journal(root, task, "CommitToGit pushed to remote.")

    return StageReport(
        task_id=task.id,
        step="commit_to_git",
        verdict="pass",
        summary="CommitToGit created and integrated the final completion commit",
        warnings=push_warning,
    )


def _unexpected_dirty_paths(
    dirty_entries: list[str], allowed_paths: set[PurePosixPath]
) -> list[str]:
    unexpected: list[str] = []
    for entry in dirty_entries:
        path = _status_entry_path(entry)
        if path is None:
            continue
        if _is_allowed_commit_path(path, allowed_paths):
            continue
        if path.startswith(".litehive/"):
            continue
        if path.startswith("$tmpdir/.litehive/"):
            continue
        if path.startswith('"$tmpdir"/.litehive/'):
            continue
        unexpected.append(path)
    return unexpected


def _commit_to_git_failure_diagnostics(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    *,
    phase: str,
    error_text: str | None,
    base_sha: str | None = None,
    message: str | None = None,
    dirty_entries: list[str] | None = None,
    attempt: int | None = None,
) -> dict[str, str | int | bool | None | list[str]]:
    worktree_path = get_task_worktree_path(task) or (
        str(execution_root.relative_to(root))
        if execution_root != root and execution_root.is_relative_to(root)
        else str(execution_root)
    )
    diagnostics: dict[str, str | int | bool | None | list[str]] = {
        "phase": phase,
        "workspace_root": str(root),
        "execution_root": str(execution_root),
        "worktree_path": worktree_path,
        "checkpoint_attempt": attempt if attempt is not None else task.git.checkpoint_attempts,
        "planned_checkpoint_attempt": attempt if attempt is not None else task.git.checkpoint_attempts + 1,
        "checkpoint_base_sha": base_sha if base_sha is not None else task.git.checkpoint_base_sha,
        "planned_commit_message": message if message is not None else checkpoint_message(
            task,
            attempt=(attempt if attempt is not None else task.git.checkpoint_attempts + 1),
        ),
    }
    if dirty_entries is not None:
        diagnostics["dirty_paths"] = [
            path for path in (_status_entry_path(entry) for entry in dirty_entries) if path
        ]
        diagnostics["dirty_entry_count"] = len(dirty_entries)
    if error_text is not None:
        diagnostics["error"] = error_text
    return diagnostics


def _allowed_commit_paths(root: Path, task: TaskRecord) -> set[PurePosixPath]:
    allowed = {
        PurePosixPath(".litehive") / ".gitignore",
        PurePosixPath(".litehive") / "config.yaml",
        PurePosixPath(".litehive") / "context.md",
        PurePosixPath(".litehive") / "state.yaml",
        PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}",
    }
    reports_dir = root / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    for report_path in reports_dir.glob("*.yaml"):
        report_data = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        for changed in report_data.get("files_changed") or []:
            normalized = str(changed).strip()
            if normalized and normalized.lower() not in {"none", "n/a", "-", "path/to/file"} and "none" not in normalized.lower():
                allowed.add(PurePosixPath(normalized))
    return allowed


def _is_allowed_commit_path(path: str, allowed_paths: set[PurePosixPath]) -> bool:
    candidate = PurePosixPath(path)
    for allowed in allowed_paths:
        if candidate == allowed or allowed in candidate.parents:
            return True
    return False


def _status_entry_path(entry: str) -> str | None:
    if len(entry) < 4:
        return None
    path = entry[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    normalized = path.strip().replace('\\"', '"')
    if normalized.startswith('"') and normalized.endswith('"') and len(normalized) >= 2:
        normalized = normalized[1:-1]
    return normalized.strip() or None


def _dirty_entry_paths(entries: list[str]) -> list[str]:
    paths = [path for entry in entries if (path := _status_entry_path(entry)) is not None]
    return sorted(dict.fromkeys(paths))
