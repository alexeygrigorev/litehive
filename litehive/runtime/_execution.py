"""Core task execution, pool orchestration, hooks, and dirty-worktree inspection."""

import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Callable

import yaml

from litehive.config import LitehiveConfig, load_config, load_context
from litehive.engines import extract_engine_continuation, get_engine
from litehive.engines._codex_quota import check_codex_quota, codex_quota_block_reason
from litehive.git_ops import (
    GitError,
    add_worktree,
    current_head,
    has_changes,
    is_git_repo,
    rebase_worktree_onto,
    status_porcelain,
)
from litehive.models import StageReport, TaskRecord, cap_feedback
from litehive.runner import RunResult, StageExecutor, TaskExecutionRunner
from litehive.subagents import SubagentManager, stage_prompt, stage_report_from_subagent
from litehive.tasks import (
    _atomic_write_gzip_text,
    _atomic_write_text,
    BlockedTask,
    active_task_markers,
    append_journal,
    dequeue_next_task,
    dequeue_next_task_selection,
    get_task,
    get_task_worktree_path,
    list_tasks,
    load_state,
    mark_engine_switch,
    mark_task_run_started,
    peek_next_task,
    peek_next_task_selection,
    persist_task_and_state,
    recover_stale_runner_state,
    restore_untouched_active_task,
    runner_heartbeat,
    save_task,
    set_pool_stop_reason,
    set_task_worktree_path,
    task_dir,
    workspace_runner_guard,
    WorkspaceConflictError,
)

from ._budget import (
    _budget_ledger_from_config,
    _budget_ledger_from_conditions,
    _count_execution_limits,
    _engine_attempt_order,
    _execution_exhausted_limit_fallbacks,
    _execution_hit_limit,
    _execution_limit_kind,
    _limit_stop_condition_is_configured,
)
from ._commit import _commit_to_git_report
from ._models import (
    _resolve_stage_retry_limit,
    _retry_backoff_seconds,
    _role_for_step,
    _set_continuation_handoff,
    resolve_engine_plan,
    resolve_execution_retry_policy,
    resolve_model,
    resolve_task_retry_policy,
)
from ._recovery import _attempt_stage_recovery
from ._types import (
    DirtyWorktreeFinding,
    DirtyWorktreeGateReport,
    EngineBudgetLedger,
    ExecutionSummary,
    SingleTaskRunSummary,
    TaskPoolRunSummary,
    TaskPoolStopConditions,
    _path_within,
)

_COMPRESS_HOOK_ARTIFACT_MIN_BYTES = 4096

_PRE_STAGE_HOOK_POINTS = {
    "implementing": "before_swe_implementation",
    "accepting": "before_pm_acceptance",
}
_POST_STAGE_HOOK_POINTS = {
    "implementing": "after_swe_implementation",
}
_POST_ACCEPT_VERDICTS = {"pass", "accept"}


def _record_codex_monitoring(root: Path, status: object) -> None:
    try:
        from litehive.observability._engine_monitoring import record_codex_quota_check

        record_codex_quota_check(root, status=status)
    except Exception:  # noqa: BLE001
        pass  # best-effort monitoring


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
        execution_root = _resolve_task_execution_root(root, task, config=config)
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
        if stop_reason.startswith("human_checkpoint_") or stop_reason == "task_requeued":
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
            stop_reason = _pool_stop_reason(
                root, executions, conditions, budget_ledger=budget_ledger
            )
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
    if (
        conditions.stop_on_failure
        and final_status is not None
        and final_status not in {"done", "paused"}
    ):
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
        if finding.location_kind == "main-checkout"
        and finding.ownership == "task-owned"
        and finding.task_id
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


def _dirty_entry_paths(dirty_entries: list[str]) -> list[str]:
    """Extract bare paths from git status porcelain output lines."""
    paths = []
    for entry in dirty_entries:
        if len(entry) < 3:
            continue
        raw = entry[3:].strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"')
        if raw:
            paths.append(raw)
    return paths


def _allowed_commit_paths(root: Path, task: TaskRecord) -> set[PurePosixPath]:
    """Return paths expected to be dirty at commit time for this task.

    Includes the task's own .litehive directory and any files listed in
    stage reports under files_changed (placeholder entries are filtered out).
    """
    _PLACEHOLDERS = {"none", "n/a", "-", ""}
    paths: set[PurePosixPath] = set()
    paths.add(PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}")
    reports_dir = root / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    if reports_dir.exists():
        for report_file in sorted(reports_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(report_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                for f in data.get("files_changed", []) or []:
                    stripped = str(f).strip().strip("/")
                    if stripped.lower() not in _PLACEHOLDERS:
                        paths.add(PurePosixPath(stripped))
            except Exception:
                pass
    return paths


def _unexpected_dirty_paths(
    dirty_entries: list[str],
    allowed_paths: set[PurePosixPath],
) -> list[str]:
    """Return dirty paths that are not covered by the allowed set.

    Workspace-internal churn under .litehive/ and stray tmp-path deletions
    are always ignored regardless of the allowed set.
    """
    unexpected = []
    for entry in dirty_entries:
        if len(entry) < 3:
            continue
        raw = entry[3:].strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"')
        if not raw:
            continue
        # Ignore stray tmpdir cleanup entries (deleted temp workspaces)
        if "$tmpdir" in raw or raw.startswith("/tmp/"):
            continue
        # Ignore all .litehive/ workspace-internal churn
        if raw.startswith(".litehive/"):
            if not any(raw == str(p) or raw.startswith(str(p) + "/") for p in allowed_paths):
                continue
        path = PurePosixPath(raw)
        if any(raw == str(p) or raw.startswith(str(p) + "/") for p in allowed_paths):
            continue
        unexpected.append(raw)
    return unexpected


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


def _run_worktree_merge_agent(
    root: Path, worktree_path: Path, task: TaskRecord, main_head: str,
    *, config: "LitehiveConfig | None" = None,
) -> None:
    """Merge main into worktree, launching a merge agent on conflict."""
    merge = subprocess.run(
        ["git", "merge", main_head, "--no-edit"],
        cwd=worktree_path, capture_output=True, text=True,
    )
    if merge.returncode == 0:
        append_journal(root, task, "[worktree] Merged main into worktree.")
        return

    # Merge conflict — find conflicting files and launch agent
    conflict_proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=worktree_path, capture_output=True, text=True,
    )
    conflicts = [f.strip() for f in conflict_proc.stdout.splitlines() if f.strip()]
    if not conflicts:
        subprocess.run(["git", "merge", "--abort"], cwd=worktree_path, capture_output=True)
        append_journal(root, task, f"[worktree] Merge failed (no conflict files detected): {merge.stderr.strip()}")
        return

    append_journal(root, task, f"[worktree] Merge conflict on {len(conflicts)} file(s). Launching merge agent.")
    cfg = config or load_config(root)
    engine_name = cfg.recovery_engine or task.engine or cfg.default_engine
    model = resolve_model(task, cfg, engine_name=engine_name)
    subagents = SubagentManager(root, execution_root=worktree_path)
    subagents.run(
        task, role="merge-resolver", engine_name=engine_name, model=model,
        prompt=(
            f"Git merge conflict while updating task {task.id} worktree to latest main.\n"
            f"Conflicting files: {', '.join(conflicts)}\n\n"
            f"Resolution rules:\n"
            f"- Preserve BOTH sides' intent — combine changes, don't pick one side.\n"
            f"- Main branch has latest infrastructure. Worktree has task's feature code.\n"
            f"- Never silently drop changes from either side.\n\n"
            f"After resolving: git add the files, then git commit --no-edit.\n"
        ),
    )
    # Check if resolved
    remaining = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=worktree_path, capture_output=True, text=True,
    )
    if not remaining.stdout.strip():
        append_journal(root, task, "[worktree] Merge agent resolved conflicts.")
    else:
        subprocess.run(["git", "merge", "--abort"], cwd=worktree_path, capture_output=True)
        append_journal(root, task, "[worktree] Merge agent could not resolve. Worktree kept as-is.")


def _resolve_task_execution_root(root: Path, task: TaskRecord, *, config: "LitehiveConfig | None" = None) -> Path:
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
                rebased = rebase_worktree_onto(worktree_path, main_head)
                if not rebased:
                    # Rebase failed — launch merge agent to resolve
                    append_journal(root, task, f"[worktree] Rebase onto {main_head[:8]} failed. Launching merge agent.")
                    _run_worktree_merge_agent(root, worktree_path, task, main_head, config=config)
            return worktree_path

    worktree_path = _task_worktree_path(root, task)  # noqa: E305
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        shutil.rmtree(worktree_path)
    add_worktree(root, worktree_path, ref=current_head(root) or "HEAD")
    set_task_worktree_path(task, str(worktree_path.relative_to(root)))
    save_task(root, task)
    append_journal(root, task, f"Created task worktree at `{get_task_worktree_path(task)}`.")
    return worktree_path


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
            append_journal(
                root,
                latest.task,
                f"Pool stopped: {stop_reason}. Awaiting human review at {checkpoint}.",
            )
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
                        f"  litehive report --verdict <pass|fail|reject> --role {role_name} "
                        f'--step {step} --message "<your detailed report>"\n\n'
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
    report.feedback = cap_feedback(
        "\n\n".join(
            [
                *_flatten_runner_hook_feedback(hook_results),
                report.feedback,
            ]
        ).strip()
    )


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
    hook_results: list[dict[str, str | int | bool | None]],
) -> list[str]:
    warnings: list[str] = []
    for hook_result in hook_results:
        warnings.extend(_runner_hook_warnings(hook_result))
    return warnings


def _flatten_runner_hook_feedback(
    hook_results: list[dict[str, str | int | bool | None]],
) -> list[str]:
    return [_runner_hook_feedback(hook_result) for hook_result in hook_results]
