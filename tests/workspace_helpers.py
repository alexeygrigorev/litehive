import argparse
import gzip
import os
from pathlib import Path, PurePosixPath
import shutil as _shutil
import subprocess
import sys
import tempfile as _tempfile
import threading
import time

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import litehive.tasks as tasks_module
from litehive.cli import (
    _cmd_abandon_task,
    _cmd_add,
    _cmd_archive,
    _cmd_cleanup,
    _cmd_close_task,
    _cmd_debug,
    _cmd_dirty_worktree_gate,
    _cmd_intake,
    _cmd_issue,
    _cmd_list,
    _cmd_move,
    _cmd_prioritize,
    _cmd_promote,
    _cmd_report,
    _cmd_queue,
    _cmd_recover,
    _cmd_repair,
    _cmd_requeue_task,
    _cmd_resume_task,
    _cmd_rollback,
    _cmd_run,
    _cmd_show,
    _cmd_status,
    _cmd_stop_task,
    _cmd_switch_task,
    _cmd_update,
    build_parser,
)
from litehive.config import (
    ExternalEngineSandboxConfig,
    ExternalEngineSandboxPolicy,
    LitehiveConfig,
    SandboxCredentialInput,
    SubagentResourceLimitsConfig,
    available_process_profiles,
    ensure_workspace,
    format_external_engine_sandbox,
    format_subagent_resource_limits,
    global_config_path,
    load_config,
    render_context_template,
    resolve_process_profile,
)
from litehive.engines import (
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
    extract_engine_continuation,
    extract_engine_timeline,
    get_engine,
)
from litehive.engines.base import (
    AdapterCapabilities,
    CLIExecutionResult,
    ExternalCLIAdapter,
    parse_stage_report_text,
)
from litehive.engines.sandbox import SandboxLauncher
from litehive.git_ops import GitError, checkpoint_message, commit_task
from litehive.models import (
    EngineUsageObservation,
    EngineUsageWindow,
    FollowUpTaskSpec,
    LiveEvent,
    LiveTimeline,
    ResourceLimitEvent,
    RuntimeContinuationHandoff,
    RuntimeEngineContinuation,
    RuntimeInterruptionState,
    RuntimeStageState,
    RuntimeSubagentState,
    RunnerStatusState,
    StageReport,
    SubagentRef,
    TaskRecord,
    UpstreamContributionOrigin,
    UpstreamPatchProposal,
)
from litehive.observability import (
    load_engine_monitoring,
    record_engine_execution,
    render_task_summary,
)
from litehive.runner import TaskExecutionRunner
from litehive.runtime import (
    EngineBudgetLedger,
    TaskPoolStopConditions,
    _allowed_commit_paths,
    _commit_to_git_report,
    _role_for_step,
    _unexpected_dirty_paths,
    drain_task_pool,
    recover_completed_task,
    resolve_engine_name,
    resolve_engine_plan,
    resolve_execution_retry_policy,
    resolve_model,
    resolve_next_task,
    rollback_completed_task,
    run_next_task,
    run_single_task,
    run_task,
)
from litehive.subagents import (
    EngineFailure,
    SubagentManager,
    SubagentResult,
    intake_prompt,
    stage_prompt,
    stage_report_from_subagent,
)
from litehive.tasks import (
    WorkspaceConflictError,
    abandon_task,
    archive_done_tasks,
    archive_root,
    archive_task,
    cleanup_archived_tasks,
    close_task,
    create_follow_up_tasks,
    create_task,
    dequeue_next_task_selection,
    finish_task_run_transition,
    get_task,
    get_task_worktree_path,
    implementation_entry_stage,
    list_archived_tasks,
    list_tasks,
    load_state,
    mark_subagent_started,
    move_queued_task,
    needs_normalization,
    peek_next_task_selection,
    recover_stale_runner_state,
    requeue_task,
    repair_workspace_state,
    require_task,
    reroute_stage_for_acceptance_criteria,
    restore_untouched_active_task,
    resume_task,
    runner_status,
    save_state,
    save_task,
    save_task_runtime,
    set_active_task,
    stop_current_task,
    switch_task_engine,
    task_dir,
    task_file,
    task_requires_acceptance_criteria,
    task_runtime_file,
    update_task_metadata,
)
from litehive.web import build_workspace_snapshot, read_session_view




def _block_runner_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    real_flock = tasks_module.fcntl.flock

    def fake_flock(fd, flags):  # type: ignore[no-untyped-def]
        if flags & tasks_module.fcntl.LOCK_NB:
            raise BlockingIOError("runner is busy")
        return real_flock(fd, flags)

    monkeypatch.setattr("litehive.tasks.fcntl.flock", fake_flock)


def _fail_atomic_write_on_path(
    monkeypatch: pytest.MonkeyPatch, failing_path: Path, message: str = "write failed"
) -> None:
    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_selected_write(path: Path, content: str) -> None:
        if path == failing_path:
            raise OSError(message)
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_selected_write)


def _latest_pool_run_report(root: Path) -> dict[str, object]:
    reports = sorted((root / ".litehive" / "logs" / "pool-runs").glob("*.yaml"))
    assert reports
    return yaml.safe_load(reports[-1].read_text(encoding="utf-8")) or {}


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _git_status_without_litehive(cwd: Path) -> list[str]:
    status = _run(["git", "status", "--short"], cwd)
    return [line for line in status.splitlines() if line and ".litehive/" not in line and not line.endswith(".litehive")]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _with_fake_uv(fake_uv: Path, *, xdg_config_home: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{fake_uv.parent}:{env['PATH']}"
    if xdg_config_home is not None:
        env["XDG_CONFIG_HOME"] = str(xdg_config_home)
    return env


def _write_fake_uv(tmp_path: Path, script: str) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(script, encoding="utf-8")
    fake_uv.chmod(0o755)
    return fake_uv

_GIT_TEMPLATE_DIR: Path | None = None
_GIT_TEMPLATE_SHA: str = ""


def _get_git_template() -> tuple[Path, str]:
    """Create a reusable template git repo (once per process)."""
    global _GIT_TEMPLATE_DIR, _GIT_TEMPLATE_SHA
    if _GIT_TEMPLATE_DIR is not None and _GIT_TEMPLATE_DIR.exists():
        return _GIT_TEMPLATE_DIR, _GIT_TEMPLATE_SHA
    tpl = Path(_tempfile.mkdtemp(prefix="litehive-git-tpl-"))
    _run(["git", "init"], tpl)
    _run(["git", "config", "user.name", "Litehive Tests"], tpl)
    _run(["git", "config", "user.email", "tests@example.com"], tpl)
    (tpl / "app.txt").write_text("base\n", encoding="utf-8")
    _run(["git", "add", "app.txt"], tpl)
    _run(["git", "commit", "-m", "initial"], tpl)
    _GIT_TEMPLATE_SHA = _run(["git", "rev-parse", "HEAD"], tpl)
    _GIT_TEMPLATE_DIR = tpl
    return tpl, _GIT_TEMPLATE_SHA


def _init_git_repo(tmp_path: Path) -> str:
    """Initialize a git repo at tmp_path by copying a cached template."""
    tpl, sha = _get_git_template()
    # Copy the template .git and working tree into tmp_path
    for item in tpl.iterdir():
        dst = tmp_path / item.name
        if dst.exists():
            continue
        if item.is_dir():
            _shutil.copytree(item, dst, symlinks=True)
        else:
            _shutil.copy2(item, dst)
    return sha


def _commit_repo_state(cwd: Path, message: str = "baseline") -> str:
    _run(["git", "add", "-A"], cwd)
    _run(["git", "commit", "-m", message], cwd)
    return _run(["git", "rev-parse", "HEAD"], cwd)


def _resolve_workspace_root(path: Path) -> Path:
    """Resolve back to the main workspace root if path is inside a worktree."""
    # Check if path is inside a .litehive/worktrees/<task>/ directory.
    parts = path.parts
    for i, part in enumerate(parts):
        if part == ".litehive" and i + 2 < len(parts) and parts[i + 1] == "worktrees":
            # Main workspace root is the parent of .litehive/
            return Path(*parts[:i])
    return path


def _write_cli_verdict(
    root: Path,
    task: "TaskRecord",
    step: str,
    verdict: str = "pass",
    message: str | None = None,
    files_changed: list[str] | None = None,
) -> None:
    """Simulate a litehive report CLI invocation by writing a thread comment.

    The ``files_changed`` parameter is accepted for backward compatibility but
    ignored — git is the source of truth for changed files.
    """
    from litehive.models import TaskThreadComment

    # Write to the main workspace root, not the worktree.
    ws_root = _resolve_workspace_root(root)
    task_dir = tasks_module.task_dir(ws_root, task)
    task_dir.mkdir(parents=True, exist_ok=True)

    # files_changed is no longer passed by agents; it is populated from git diff
    # at commit time. The parameter is kept for backward compatibility but ignored.
    tasks_module.append_thread_comment(
        ws_root,
        task,
        TaskThreadComment(
            role="swe",
            step=step,
            verdict=verdict,
            message=message or f"{step} {verdict}",
        ),
    )


def _completed_subagent_result(
    tmp_path: Path, step: str, *, engine_name: str = "codex", task: "TaskRecord | None" = None
) -> SubagentResult:
    worktrees_root = tmp_path / ".litehive" / "worktrees"
    if step == "implementing" and worktrees_root.exists():
        wrote_to_worktree = False
        main_app = tmp_path / "app.txt"
        for worktree in sorted(worktrees_root.iterdir()):
            if not worktree.is_dir():
                continue
            worktree_app = worktree / "app.txt"
            if main_app.exists() and worktree_app.exists():
                # Move main's dirty content (if any) to the worktree to avoid
                # merge conflicts, then reset main.  If main is clean, append
                # a line so the empty SWE guard still detects a change via git.
                main_content = main_app.read_text(encoding="utf-8")
                head_content = subprocess.run(
                    ["git", "show", "HEAD:app.txt"],
                    cwd=tmp_path, capture_output=True, text=True,
                ).stdout
                if main_content != head_content:
                    # Main has uncommitted changes — transfer them to worktree
                    worktree_app.write_text(main_content, encoding="utf-8")
                    subprocess.run(["git", "checkout", "--", "app.txt"], cwd=tmp_path, check=True)
                else:
                    # Main is clean — write a synthetic change
                    worktree_app.write_text(main_content + "implemented\n", encoding="utf-8")
                wrote_to_worktree = True
                break
        if not wrote_to_worktree:
            # No worktree — write a change in the main repo so the git-based guard detects it.
            app_file = tmp_path / "app.txt"
            app_file.write_text("implemented\n", encoding="utf-8")
    elif step == "implementing":
        # No worktrees dir — write a change in the main repo so the git-based guard detects it.
        app_file = tmp_path / "app.txt"
        app_file.write_text("implemented\n", encoding="utf-8")

    # Simulate CLI verdict submission via thread comment.
    if task is not None:
        _write_cli_verdict(
            tmp_path,
            task,
            step,
            verdict="pass",
            message=f"{step} complete via {engine_name}",
        )

    return SubagentResult(
        ref=SubagentRef(
            id=f"SA-{step}",
            role="swe",
            engine=engine_name,
            status="completed",
            path=f"subagents/{step}",
        ),
        execution=CLIExecutionResult(
            adapter=engine_name,
            argv=(engine_name, "exec"),
            cwd=tmp_path,
            exit_code=0,
            stdout=f"{step} complete via {engine_name}\n",
            stderr="",
        ),
        transcript="",
        exit_code=0,
    )


def _failed_subagent_result(
    tmp_path: Path, step: str, *, engine_name: str = "codex", task: "TaskRecord | None" = None
) -> SubagentResult:
    # Simulate CLI verdict submission via thread comment.
    if task is not None:
        _write_cli_verdict(
            tmp_path,
            task,
            step,
            verdict="fail",
            message=f"recovery failed for {step}",
        )

    return SubagentResult(
        ref=SubagentRef(
            id=f"SA-{step}-recovery",
            role="recovery",
            engine=engine_name,
            status="failed",
            path=f"subagents/{step}-recovery",
        ),
        execution=CLIExecutionResult(
            adapter=engine_name,
            argv=(engine_name, "exec"),
            cwd=tmp_path,
            exit_code=1,
            stdout=f"recovery failed for {step}\n",
            stderr="",
        ),
        transcript="",
        exit_code=1,
    )


def _stage_subagent_result(
    cwd: Path,
    step: str,
    *,
    role: str = "swe",
    engine_name: str = "codex",
    verdict: str = "PASS",
    summary: str | None = None,
    files_changed: list[str] | None = None,  # ignored — git is source of truth
    tests_added: int = 1,
    tests_passing: int = 1,
    warnings: list[str] | None = None,
    task: "TaskRecord | None" = None,
) -> SubagentResult:
    effective_summary = summary or f"{step} complete via {engine_name}"
    effective_verdict = verdict.lower()

    # Simulate CLI verdict submission via thread comment.
    if task is not None:
        _write_cli_verdict(
            cwd,
            task,
            step,
            verdict=effective_verdict,
            message=effective_summary,
        )

    transcript = f"{effective_summary}\n"
    return SubagentResult(
        ref=SubagentRef(
            id=f"SA-{step}-{engine_name}",
            role=role,
            engine=engine_name,
            status="completed",
            path=f"subagents/{step}-{engine_name}",
        ),
        execution=CLIExecutionResult(
            adapter=engine_name,
            argv=(engine_name, "exec"),
            cwd=cwd,
            exit_code=0,
            stdout=transcript,
            stderr="",
        ),
        transcript=transcript,
        exit_code=0,
    )


def _resource_limited_subagent_result(
    tmp_path: Path,
    step: str,
    *,
    engine_name: str = "codex",
    resource: str = "memory",
    reason: str = "memory limit exceeded (OOM)",
) -> SubagentResult:
    event = ResourceLimitEvent(
        resource=resource,  # type: ignore[arg-type]
        reason=reason,
        observed_signal="oom",
        exit_code=137,
        memory_mb=4096,
        cpu_count=2.0,
        process_limit=256,
    )
    return SubagentResult(
        ref=SubagentRef(
            id=f"SA-{step}",
            role="swe",
            engine=engine_name,
            status="failed",
            path=f"subagents/{step}",
        ),
        execution=CLIExecutionResult(
            adapter=engine_name,
            argv=(engine_name, "exec"),
            cwd=tmp_path,
            exit_code=137,
            stdout="compiler terminated",
            stderr="OOMKilled: container exceeded memory limit",
        ),
        transcript="[stderr]\nOOMKilled: container exceeded memory limit",
        exit_code=137,
        failure=EngineFailure(
            kind="resource_limit",
            reason=reason,
            classification=resource,
            resource_limit_event=event,
        ),
    )


def _interrupted_subagent_result(
    tmp_path: Path, step: str, *, engine_name: str = "codex"
) -> SubagentResult:
    return SubagentResult(
        ref=SubagentRef(
            id=f"SA-{step}",
            role="swe",
            engine=engine_name,
            status="interrupted",
            path=f"subagents/{step}",
        ),
        execution=CLIExecutionResult(
            adapter=engine_name,
            argv=(engine_name, "exec"),
            cwd=tmp_path,
            exit_code=130,
            stdout="Execution interrupted by user",
            stderr="received SIGINT",
        ),
        transcript="Execution interrupted by user\n\n[stderr]\nreceived SIGINT",
        exit_code=130,
        failure=EngineFailure(
            kind="execution_interrupted",
            reason="execution interrupted",
        ),
    )


def _successful_stage_execution(tmp_path: Path, adapter: str, step: str) -> CLIExecutionResult:
    # Auto-write a CLI verdict for the active task in the workspace.
    ws_root = _resolve_workspace_root(tmp_path)
    from litehive.tasks import load_state as _load_state, get_task as _get_task

    state = _load_state(ws_root)
    active_id = state.active_task_id
    if active_id:
        active_task = _get_task(ws_root, active_id)
        if active_task is not None:
            _write_cli_verdict(
                ws_root,
                active_task,
                active_task.pipeline_status,
                verdict="pass",
                message=f"{step} complete via {adapter}",
            )
    return CLIExecutionResult(
        adapter=adapter,
        argv=(adapter, "exec"),
        cwd=tmp_path,
        exit_code=0,
        stdout=f"{step} complete via {adapter}\n",
        stderr="",
    )


__all__ = [
    "argparse",
    "gzip",
    "os",
    "Path",
    "PurePosixPath",
    "subprocess",
    "sys",
    "threading",
    "time",
    "pytest",
    "yaml",
    "tasks_module",
    "_cmd_abandon_task",
    "_cmd_add",
    "_cmd_archive",
    "_cmd_cleanup",
    "_cmd_close_task",
    "_cmd_debug",
    "_cmd_dirty_worktree_gate",
    "_cmd_intake",
    "_cmd_issue",
    "_cmd_list",
    "_cmd_move",
    "_cmd_prioritize",
    "_cmd_promote",
    "_cmd_report",
    "_cmd_queue",
    "_cmd_recover",
    "_cmd_repair",
    "_cmd_requeue_task",
    "_cmd_resume_task",
    "_cmd_rollback",
    "_cmd_run",
    "_cmd_show",
    "_cmd_status",
    "_cmd_stop_task",
    "_cmd_switch_task",
    "_cmd_update",
    "build_parser",
    "ExternalEngineSandboxConfig",
    "ExternalEngineSandboxPolicy",
    "LitehiveConfig",
    "SandboxCredentialInput",
    "SubagentResourceLimitsConfig",
    "available_process_profiles",
    "ensure_workspace",
    "format_external_engine_sandbox",
    "format_subagent_resource_limits",
    "global_config_path",
    "load_config",
    "render_context_template",
    "resolve_process_profile",
    "classify_execution_interruption",
    "classify_execution_limit",
    "classify_retryable_execution_failure",
    "extract_engine_continuation",
    "extract_engine_timeline",
    "get_engine",
    "AdapterCapabilities",
    "CLIExecutionResult",
    "ExternalCLIAdapter",
    "parse_stage_report_text",
    "SandboxLauncher",
    "GitError",
    "checkpoint_message",
    "commit_task",
    "EngineUsageObservation",
    "EngineUsageWindow",
    "FollowUpTaskSpec",
    "LiveEvent",
    "LiveTimeline",
    "ResourceLimitEvent",
    "RuntimeContinuationHandoff",
    "RuntimeEngineContinuation",
    "RuntimeInterruptionState",
    "RuntimeStageState",
    "RuntimeSubagentState",
    "RunnerStatusState",
    "StageReport",
    "SubagentRef",
    "TaskRecord",
    "UpstreamContributionOrigin",
    "UpstreamPatchProposal",
    "load_engine_monitoring",
    "record_engine_execution",
    "render_task_summary",
    "TaskExecutionRunner",
    "EngineBudgetLedger",
    "TaskPoolStopConditions",
    "_allowed_commit_paths",
    "_commit_to_git_report",
    "_role_for_step",
    "_unexpected_dirty_paths",
    "drain_task_pool",
    "recover_completed_task",
    "resolve_engine_name",
    "resolve_engine_plan",
    "resolve_execution_retry_policy",
    "resolve_model",
    "resolve_next_task",
    "rollback_completed_task",
    "run_next_task",
    "run_single_task",
    "run_task",
    "EngineFailure",
    "SubagentManager",
    "SubagentResult",
    "intake_prompt",
    "stage_prompt",
    "stage_report_from_subagent",
    "WorkspaceConflictError",
    "abandon_task",
    "archive_done_tasks",
    "archive_root",
    "archive_task",
    "cleanup_archived_tasks",
    "close_task",
    "create_follow_up_tasks",
    "create_task",
    "dequeue_next_task_selection",
    "finish_task_run_transition",
    "get_task",
    "get_task_worktree_path",
    "implementation_entry_stage",
    "list_archived_tasks",
    "list_tasks",
    "load_state",
    "mark_subagent_started",
    "move_queued_task",
    "needs_normalization",
    "peek_next_task_selection",
    "recover_stale_runner_state",
    "requeue_task",
    "repair_workspace_state",
    "require_task",
    "reroute_stage_for_acceptance_criteria",
    "restore_untouched_active_task",
    "resume_task",
    "runner_status",
    "save_state",
    "save_task",
    "save_task_runtime",
    "set_active_task",
    "stop_current_task",
    "switch_task_engine",
    "task_dir",
    "task_file",
    "task_requires_acceptance_criteria",
    "task_runtime_file",
    "update_task_metadata",
    "build_workspace_snapshot",
    "read_session_view",
    "_block_runner_lock",
    "_fail_atomic_write_on_path",
    "_latest_pool_run_report",
    "_run",
    "_git_status_without_litehive",
    "_repo_root",
    "_with_fake_uv",
    "_write_fake_uv",
    "_get_git_template",
    "_init_git_repo",
    "_commit_repo_state",
    "_resolve_workspace_root",
    "_write_cli_verdict",
    "_completed_subagent_result",
    "_failed_subagent_result",
    "_stage_subagent_result",
    "_resource_limited_subagent_result",
    "_interrupted_subagent_result",
    "_successful_stage_execution",
]
