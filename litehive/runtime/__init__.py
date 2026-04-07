"""High-level runtime flow for executing queued tasks."""

import subprocess
import time

from litehive.subagents import SubagentManager

from ._types import (
    DirtyWorktreeFinding,
    DirtyWorktreeGateReport,
    EngineBudgetLedger,
    ExecutionSummary,
    ResolvedExecutionRetryPolicy,
    RollbackSummary,
    SingleTaskRunSummary,
    TaskPoolRunSummary,
    TaskPoolStopConditions,
    _path_within,
)

from ._budget import (
    _budget_ledger_from_conditions,
    _budget_ledger_from_config,
    _count_execution_limits,
    _engine_attempt_order,
    _execution_exhausted_limit_fallbacks,
    _execution_hit_limit,
    _execution_limit_kind,
    _limit_stop_condition_is_configured,
)

from ._models import (
    _execution_retry_model_family,
    _is_recovery_run,
    _resolve_stage_retry_limit,
    _retry_backoff_seconds,
    _role_for_step,
    _set_continuation_handoff,
    active_engine_freezes,
    is_engine_frozen,
    resolve_engine_attempt_order,
    resolve_engine_name,
    resolve_engine_plan,
    resolve_execution_retry_policy,
    resolve_model,
    resolve_task_retry_policy,
    workspace_model_for_engine,
)

from ._recovery import (
    _attempt_commit_recovery,
    _attempt_stage_recovery,
    _capture_persisted_files,
    _classify_recovery_failure_owner,
    _require_completed_task,
    _resolve_recovery_engine,
    _restore_persisted_files,
    _traceback_fingerprint,
    _traceback_frame_paths,
    _traceback_text,
    recover_completed_task,
    rollback_completed_task,
)

from ._commit import _commit_to_git_report

from ._execution import (
    _allowed_commit_paths,
    _attach_runner_hook_results,
    _dirty_entry_paths,
    _dirty_worktree_owner_task,
    _execute_runner_hook,
    _finalize_pool_run,
    _flatten_runner_hook_feedback,
    _flatten_runner_hook_warnings,
    _git_worktree_blocks_pool,
    _git_worktree_is_dirty,
    _human_checkpoint_stop_reason,
    _pool_stop_reason,
    _requires_continue_or_rollback,
    _resolve_task_execution_root,
    _run_runner_hooks_for_stage,
    _runner_hook_feedback,
    _runner_hook_point,
    _runner_hook_warnings,
    _single_task_pre_stop_reason,
    _single_task_stop_reason,
    _task_can_resume_with_owned_dirty_paths,
    _task_worktree_path,
    _unexpected_dirty_paths,
    build_executor,
    drain_task_pool,
    inspect_dirty_worktree_gate,
    resolve_next_task,
    run_next_task,
    run_next_task_with_override,
    run_single_task,
    run_task,
)

__all__ = [
    # types
    "DirtyWorktreeFinding",
    "DirtyWorktreeGateReport",
    "EngineBudgetLedger",
    "ExecutionSummary",
    "ResolvedExecutionRetryPolicy",
    "RollbackSummary",
    "SingleTaskRunSummary",
    "TaskPoolRunSummary",
    "TaskPoolStopConditions",
    # budget
    "workspace_model_for_engine",
    # models
    "active_engine_freezes",
    "is_engine_frozen",
    "resolve_engine_attempt_order",
    "resolve_engine_name",
    "resolve_engine_plan",
    "resolve_execution_retry_policy",
    "resolve_model",
    "resolve_task_retry_policy",
    # recovery
    "recover_completed_task",
    "rollback_completed_task",
    # execution
    "build_executor",
    "drain_task_pool",
    "inspect_dirty_worktree_gate",
    "resolve_next_task",
    "run_next_task",
    "run_next_task_with_override",
    "run_single_task",
    "run_task",
    # re-exported for monkeypatching compatibility
    "SubagentManager",
]
