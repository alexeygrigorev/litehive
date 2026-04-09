"""Parallel task execution: run multiple independent tasks concurrently in separate worktrees."""

import logging
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from litehive.config import load_config, load_context, LitehiveConfig
from litehive.models import TaskRecord
from litehive.agents import SubagentManager
from litehive.pipeline.core import TaskExecutionRunner
from litehive.tasks import (
    BlockedTask,
    append_journal,
    mark_task_run_started,
    recover_stale_runner_state,
    runner_heartbeat,
    set_pool_stop_reason,
    workspace_runner_guard,
)

from ._budget import _budget_ledger_from_conditions, _budget_ledger_from_config
from ._builder import build_executor
from ._models import (
    _resolve_stage_retry_limit,
    resolve_engine_plan,
    resolve_model,
    resolve_task_retry_policy,
)
from ._types import (
    EngineBudgetLedger,
    ExecutionSummary,
    TaskPoolStopConditions,
)
from ._worktree import _resolve_task_execution_root

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IntegrationCheckResult:
    """Result of running a post-merge integration check on the combined state."""
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timestamp: str
    task_ids: list[str] = field(default_factory=list)
    merge_order: list[str] = field(default_factory=list)
    success: bool = True


@dataclass(slots=True)
class ParallelRunSummary:
    """Summary of a parallel task pool run."""
    executions: list[ExecutionSummary] = field(default_factory=list)
    integration_results: list["IntegrationResult"] = field(default_factory=list)
    integration_check: IntegrationCheckResult | None = None
    stop_reason: str = "queue_exhausted"
    blocked: list[BlockedTask] = field(default_factory=list)


@dataclass(slots=True)
class IntegrationResult:
    """Result of integrating a completed parallel task back to main."""
    task_id: str
    success: bool
    merge_conflict: bool = False
    conflict_resolved: bool = False
    commit_sha: str | None = None
    error: str | None = None


def _select_parallel_tasks(
    root: Path,
    capacity: int,
) -> tuple[list[TaskRecord], list[BlockedTask]]:
    """Select up to *capacity* independent tasks from the queue.

    Uses the snapshot-based selection to avoid re-entrant locking.
    Tasks that depend on each other are never both selected in
    the same batch.
    """
    from litehive.config import load_config as _load_config
    from litehive.config.constants import VALID_POOL_SELECTION_POLICIES
    from litehive.tasks import (
        load_state,
        list_tasks,
    )
    from litehive.tasks.queue_ops import (
        _resolve_next_task_from_snapshot,
    )
    from litehive.workspace.locking import _workspace_lock, workspace_mutation_guard
    from litehive.workspace.workflow import persist_tasks_and_state

    recover_stale_runner_state(root)

    selected: list[TaskRecord] = []
    blocked: list[BlockedTask] = []
    selected_ids: set[str] = set()

    with workspace_mutation_guard(root), _workspace_lock(root):
        state = load_state(root)
        tasks_by_id = {task.id: task for task in list_tasks(root)}
        config = _load_config(root)
        policy = config.pool_selection_policy
        if policy not in VALID_POOL_SELECTION_POLICIES:
            policy = "dependency_aware"

        # Simulate selection: resolve tasks one at a time from a copy of state
        simulated_state = state.model_copy(deep=True)

        for _ in range(capacity):
            next_task, next_blocked, _ = _resolve_next_task_from_snapshot(
                simulated_state, tasks_by_id, policy=policy,
            )
            if next_task is None:
                blocked = next_blocked
                break

            # Check if this task depends on an already-selected task
            if any(dep in selected_ids for dep in next_task.depends_on):
                blocked.append(BlockedTask(
                    task_id=next_task.id,
                    title=next_task.title,
                    blocked_by=[dep for dep in next_task.depends_on if dep in selected_ids],
                ))
                # Remove from simulated queue so next iteration picks a different task
                simulated_state.queue = [
                    tid for tid in simulated_state.queue if tid != next_task.id
                ]
                continue

            selected.append(next_task)
            selected_ids.add(next_task.id)

            # Mark as selected in simulation (remove from queue, clear active)
            simulated_state.active_task_id = None
            simulated_state.queue = [
                tid for tid in simulated_state.queue if tid != next_task.id
            ]

        # Apply selection to real state
        if selected:
            for task in selected:
                if task.id in state.queue:
                    state.queue = [tid for tid in state.queue if tid != task.id]
                if task.status in {"queued", "interrupted"}:
                    task.status = "in_progress"  # type: ignore[assignment]
            state.active_task_ids = [t.id for t in selected]
            state.active_task_id = selected[0].id  # Primary for backward compat
            persist_tasks_and_state(root, tasks=selected, state=state)

    return selected, blocked


def _run_single_parallel_task(
    root: Path,
    task: TaskRecord,
    *,
    engine_override: str | None = None,
    model_override: str | None = None,
    budget_ledger: EngineBudgetLedger | None = None,
) -> ExecutionSummary:
    """Execute a single task in its own worktree (called from a thread)."""
    config = load_config(root)
    workspace_context = load_context(root)
    engine_plan = resolve_engine_plan(task, config, engine_override=engine_override)
    engine_name = engine_plan[0]
    execution_root = _resolve_task_execution_root(root, task, config=config)
    subagents = SubagentManager(root, execution_root=execution_root)

    append_journal(root, task, f"[parallel] Execution started with engine `{engine_name}`.")
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
            config_auto_commit=False,  # Delay commit — integration handles it
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
        append_journal(root, task, f"[parallel] Execution finished with status `{result.final_status}`.")

    return ExecutionSummary(task=task, result=result, commit_sha=task.git.commit_sha)


def _integrate_completed_task(
    root: Path,
    execution: ExecutionSummary,
    *,
    config: LitehiveConfig | None = None,
) -> IntegrationResult:
    """Merge a completed parallel task's worktree back into main.

    This is called sequentially (deterministic order) after all parallel
    tasks finish, so the merge order is observable and reproducible.
    """
    from litehive.git import current_head, checkpoint_message, is_git_repo
    from litehive.tasks import (
        get_task_worktree_path,
        save_task,
        set_task_commit_sha,
    )

    task = execution.task
    if task is None or execution.result is None:
        return IntegrationResult(
            task_id=task.id if task else "unknown",
            success=False,
            error="no task or result",
        )

    if execution.result.final_status != "done":
        return IntegrationResult(
            task_id=task.id,
            success=False,
            error=f"task ended with status {execution.result.final_status}",
        )

    if not is_git_repo(root):
        return IntegrationResult(task_id=task.id, success=True)

    worktree_rel = get_task_worktree_path(task)
    if not worktree_rel:
        return IntegrationResult(task_id=task.id, success=True)

    execution_root = (root / worktree_rel).resolve()
    if not execution_root.exists():
        return IntegrationResult(task_id=task.id, success=True)

    commit_msg = checkpoint_message(task)

    # Commit everything in the worktree
    subprocess.run(["git", "add", "-A"], cwd=execution_root, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=execution_root, capture_output=True,
    )

    wt_head = current_head(execution_root)
    if not wt_head:
        return IntegrationResult(task_id=task.id, success=True)

    # Commit any pending state on main first
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: sync workspace state"],
        cwd=root, capture_output=True,
    )

    head_before = current_head(root)

    # Attempt merge
    merge = subprocess.run(
        ["git", "merge", wt_head, "-m", commit_msg, "--no-edit"],
        cwd=root, capture_output=True, text=True,
    )
    if merge.returncode == 0:
        head_after = current_head(root)
        # Clean up worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(execution_root)],
            cwd=root, capture_output=True,
        )
        task.git.worktree_path = None
        task.runtime.git.worktree_path = None
        task.status = "done"
        task.pipeline_status = "done"
        set_task_commit_sha(task, head_after)
        save_task(root, task)
        append_journal(root, task, f"[parallel] Integrated to main. Commit: {head_after}")

        # Push to remote
        subprocess.run(["git", "push"], cwd=root, capture_output=True, text=True)

        return IntegrationResult(
            task_id=task.id,
            success=True,
            commit_sha=head_after,
        )

    # Merge conflict — try agent resolution
    conflict_proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=root, capture_output=True, text=True,
    )
    conflicts = [f.strip() for f in conflict_proc.stdout.splitlines() if f.strip()]

    if not conflicts:
        subprocess.run(["git", "merge", "--abort"], cwd=root, capture_output=True)
        return IntegrationResult(
            task_id=task.id,
            success=False,
            merge_conflict=True,
            error="merge failed without identifiable conflict files",
        )

    append_journal(
        root, task,
        f"[parallel] Merge conflict on {len(conflicts)} file(s). Launching merge-conflict agent.",
    )

    cfg = config or load_config(root)
    engine_name = cfg.recovery_engine or task.engine or cfg.default_engine
    model = resolve_model(task, cfg, engine_name=engine_name)
    subagents = SubagentManager(root, execution_root=root)
    subagents.run(
        task,
        role="merge-resolver",
        engine_name=engine_name,
        model=model,
        prompt=(
            f"Git merge conflict while integrating parallel task {task.id} into main.\n"
            f"Conflicting files: {', '.join(conflicts)}\n\n"
            f"This is a parallel-task integration conflict — another task's changes "
            f"have already been merged to main, and this task's changes conflict.\n\n"
            f"Resolution rules:\n"
            f"- Preserve BOTH tasks' intent — combine changes, don't pick one side.\n"
            f"- Main branch has the already-integrated task's work. The merge head has this task's feature code.\n"
            f"- Never silently drop changes from either side.\n\n"
            f"After resolving: git add the resolved files, then git commit --no-edit.\n"
        ),
    )

    # Check if agent resolved it
    remaining = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=root, capture_output=True, text=True,
    )
    if not remaining.stdout.strip():
        head_after = current_head(root)
        # Clean up worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(execution_root)],
            cwd=root, capture_output=True,
        )
        task.git.worktree_path = None
        task.runtime.git.worktree_path = None
        task.status = "done"
        task.pipeline_status = "done"
        set_task_commit_sha(task, head_after)
        save_task(root, task)
        append_journal(root, task, f"[parallel] Merge agent resolved conflicts. Commit: {head_after}")

        subprocess.run(["git", "push"], cwd=root, capture_output=True, text=True)

        return IntegrationResult(
            task_id=task.id,
            success=True,
            merge_conflict=True,
            conflict_resolved=True,
            commit_sha=head_after,
        )

    # Agent could not resolve — abort merge, mark task as merge_failed
    subprocess.run(["git", "merge", "--abort"], cwd=root, capture_output=True)
    task.status = "merge_failed"
    task.pipeline_status = "commit_to_git"
    save_task(root, task)
    append_journal(root, task, "[parallel] Merge agent could not resolve conflict. Task marked merge_failed.")

    return IntegrationResult(
        task_id=task.id,
        success=False,
        merge_conflict=True,
        conflict_resolved=False,
        error=f"unresolved merge conflict on {', '.join(conflicts)}",
    )


def _run_integration_check(
    root: Path,
    command: str,
    merged_task_ids: list[str],
    merge_order: list[str],
) -> IntegrationCheckResult:
    """Run a post-merge integration check on the combined main worktree state.

    Writes a batch integration report to .litehive/logs/parallel-integration/.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    proc = subprocess.run(
        shlex.split(command),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=600,
    )
    result = IntegrationCheckResult(
        command=command,
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        timestamp=timestamp,
        task_ids=merged_task_ids,
        merge_order=merge_order,
        success=proc.returncode == 0,
    )

    # Write batch integration report
    report_dir = root / ".litehive" / "logs" / "parallel-integration"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts_slug = timestamp.replace(":", "-").replace("+", "p")
    report_path = report_dir / f"{ts_slug}-batch.yaml"

    import yaml
    report_data = {
        "command": result.command,
        "exit_code": result.exit_code,
        "success": result.success,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timestamp": result.timestamp,
        "task_ids": result.task_ids,
        "merge_order": result.merge_order,
    }
    report_path.write_text(yaml.dump(report_data, default_flow_style=False), encoding="utf-8")

    return result


def _clear_parallel_active_tasks(root: Path) -> None:
    """Remove active_task_ids from state after parallel run completes."""
    from litehive.tasks import load_state, save_state

    state = load_state(root)
    state.active_task_ids = []
    state.active_task_id = None
    save_state(root, state)


def run_parallel_tasks(
    root: Path,
    *,
    engine_override: str | None = None,
    model_override: str | None = None,
    stop_conditions: TaskPoolStopConditions | None = None,
) -> ParallelRunSummary:
    """Select and run multiple independent tasks in parallel, then integrate deterministically.

    This is task-level parallelism: each task runs in its own worktree,
    not multiple workers within one task.
    """
    root = root.resolve()
    config = load_config(root)
    capacity = config.parallel_capacity

    if capacity <= 1:
        raise ValueError(
            "parallel_capacity must be > 1 for parallel execution. "
            "Use run_single_task or drain_task_pool for sequential execution."
        )

    with workspace_runner_guard(root):
        conditions = stop_conditions or TaskPoolStopConditions()
        budget_ledger = _budget_ledger_from_conditions(conditions)
        set_pool_stop_reason(root, None)

        # Phase 1: Select tasks
        recover_stale_runner_state(root)
        tasks, blocked = _select_parallel_tasks(root, capacity)

        if not tasks:
            stop_reason = "blocked_tasks_remaining" if blocked else "queue_exhausted"
            set_pool_stop_reason(root, stop_reason)
            return ParallelRunSummary(stop_reason=stop_reason, blocked=blocked)

        logger.info(
            "Parallel run: executing %d tasks concurrently: %s",
            len(tasks),
            ", ".join(t.id for t in tasks),
        )

        # Phase 2: Execute tasks in parallel (each in its own worktree)
        executions: list[ExecutionSummary] = []
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {
                pool.submit(
                    _run_single_parallel_task,
                    root,
                    task,
                    engine_override=engine_override,
                    model_override=model_override,
                    budget_ledger=budget_ledger,
                ): task
                for task in tasks
            }
            for future in as_completed(futures):
                task = futures[future]
                try:
                    execution = future.result()
                    executions.append(execution)
                except Exception as exc:
                    logger.error("Parallel task %s failed: %s", task.id, exc)
                    executions.append(ExecutionSummary(task=task, result=None))

        # Phase 3: Integrate completed tasks back to main in deterministic order
        # Sort by task ID for reproducible merge ordering
        completed = [e for e in executions if e.result and e.result.final_status == "done"]
        completed.sort(key=lambda e: e.task.id)

        integration_results: list[IntegrationResult] = []
        for execution in completed:
            result = _integrate_completed_task(root, execution, config=config)
            integration_results.append(result)

        # Clean up parallel state
        _clear_parallel_active_tasks(root)

        # Phase 4: Post-merge integration verification
        merged_ids = [r.task_id for r in integration_results if r.success]
        merge_order = merged_ids  # Already in deterministic task-ID order
        integration_check: IntegrationCheckResult | None = None

        if config.parallel_integration_check and merged_ids:
            logger.info(
                "Running integration check: %s", config.parallel_integration_check,
            )
            integration_check = _run_integration_check(
                root,
                config.parallel_integration_check,
                merged_ids,
                merge_order,
            )
            if not integration_check.success:
                for task_id in merged_ids:
                    # Find the task and append journal entry
                    for execution in executions:
                        if execution.task and execution.task.id == task_id:
                            append_journal(
                                root, execution.task,
                                f"[parallel] Integration check failed (exit code {integration_check.exit_code}). "
                                f"Command: {integration_check.command}",
                            )
                            break

        # Determine stop reason
        all_done = all(r.success for r in integration_results) and len(integration_results) == len(tasks)
        has_conflicts = any(r.merge_conflict and not r.conflict_resolved for r in integration_results)
        has_failures = any(
            e.result and e.result.final_status not in {"done", "paused", "queued"}
            for e in executions
        )

        if has_conflicts:
            stop_reason = "parallel_integration_conflict"
        elif integration_check and not integration_check.success:
            stop_reason = "parallel_integration_failed"
        elif has_failures:
            stop_reason = "failure_detected"
        elif all_done:
            stop_reason = "parallel_batch_complete"
        else:
            stop_reason = "parallel_batch_complete"

        set_pool_stop_reason(root, stop_reason)

        return ParallelRunSummary(
            executions=executions,
            integration_results=integration_results,
            integration_check=integration_check,
            stop_reason=stop_reason,
            blocked=blocked,
        )
