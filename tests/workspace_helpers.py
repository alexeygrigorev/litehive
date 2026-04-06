import argparse

import gzip

import os

from pathlib import Path, PurePosixPath

import subprocess

import sys

import threading

import time

import pytest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import litehive.tasks as tasks_module

from litehive.cli import (
    _cmd_add,
    _cmd_issue,
    _cmd_intake,
    _cmd_abandon_task,
    _cmd_close_task,
    _cmd_dirty_worktree_gate,
    _cmd_move,
    _cmd_prioritize,
    _cmd_promote,
    _cmd_queue,
    _cmd_repair,
    _cmd_recover,
    _cmd_requeue_task,
    _cmd_resume_task,
    _cmd_rollback,
    _cmd_run,
    _cmd_stop_task,
    _cmd_status,
    _cmd_switch_task,
    _cmd_update,
    build_parser,
)

from litehive.engines import (
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
    extract_engine_continuation,
    extract_engine_timeline,
    get_engine,
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

from litehive.engine_monitoring import (
    load_engine_monitoring,
    record_engine_execution,
)

from litehive.engines.base import (
    AdapterCapabilities,
    CLIExecutionResult,
    ExternalCLIAdapter,
    parse_stage_report_text,
)

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

from litehive.observability import render_task_summary

from litehive.engines.sandbox import SandboxLauncher

from litehive.runtime import (
    EngineBudgetLedger,
    TaskPoolStopConditions,
    _commit_to_git_report,
    _role_for_step,
    _allowed_commit_paths,
    _unexpected_dirty_paths,
    drain_task_pool,
    rollback_completed_task,
    resolve_engine_plan,
    recover_completed_task,
    resolve_execution_retry_policy,
    resolve_engine_name,
    resolve_model,
    resolve_next_task,
    run_next_task,
    run_single_task,
    run_task,
)

from litehive.runner import TaskExecutionRunner

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
    close_task,
    create_follow_up_tasks,
    create_task,
    dequeue_next_task_selection,
    finish_task_run_transition,
    get_task,
    get_task_worktree_path,
    implementation_entry_stage,
    list_tasks,
    load_state,
    move_queued_task,
    mark_subagent_started,
    peek_next_task_selection,
    repair_workspace_state,
    requeue_task,
    recover_stale_runner_state,
    resume_task,
    needs_normalization,
    reroute_stage_for_acceptance_criteria,
    require_task,
    save_state,
    save_task,
    save_task_runtime,
    set_active_task,
    stop_current_task,
    switch_task_engine,
    restore_untouched_active_task,
    runner_status,
    task_dir,
    task_file,
    task_runtime_file,
    task_requires_acceptance_criteria,
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
    return [line for line in status.splitlines() if line and not line.endswith(".litehive/")]


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


def _init_git_repo(tmp_path: Path) -> str:
    _run(["git", "init"], tmp_path)
    _run(["git", "config", "user.name", "Litehive Tests"], tmp_path)
    _run(["git", "config", "user.email", "tests@example.com"], tmp_path)
    (tmp_path / "app.txt").write_text("base\n", encoding="utf-8")
    _run(["git", "add", "app.txt"], tmp_path)
    _run(["git", "commit", "-m", "initial"], tmp_path)
    return _run(["git", "rev-parse", "HEAD"], tmp_path)


def _commit_repo_state(cwd: Path, message: str = "baseline") -> str:
    _run(["git", "add", "-A"], cwd)
    _run(["git", "commit", "-m", message], cwd)
    return _run(["git", "rev-parse", "HEAD"], cwd)


def _completed_subagent_result(
    tmp_path: Path, step: str, *, engine_name: str = "codex"
) -> SubagentResult:
    worktrees_root = tmp_path / ".litehive" / "worktrees"
    if step == "implementing" and worktrees_root.exists():
        main_app = tmp_path / "app.txt"
        for worktree in sorted(worktrees_root.iterdir()):
            if not worktree.is_dir():
                continue
            worktree_app = worktree / "app.txt"
            if main_app.exists() and worktree_app.exists():
                worktree_app.write_text(main_app.read_text(encoding="utf-8"), encoding="utf-8")
                subprocess.run(["git", "checkout", "--", "app.txt"], cwd=tmp_path, check=True)
                break

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
            stdout=(
                "VERDICT: PASS\n"
                f"SUMMARY: {step} complete via {engine_name}\n"
                "FILES_CHANGED:\n"
                "- app.txt\n"
                "TESTS_ADDED: 1\n"
                "TESTS_PASSING: 1\n"
                "WARNINGS:\n"
            ),
            stderr="",
        ),
        transcript="",
        exit_code=0,
    )


def _stage_subagent_result(
    cwd: Path,
    step: str,
    *,
    role: str = "swe",
    engine_name: str = "codex",
    verdict: str = "PASS",
    summary: str | None = None,
    files_changed: list[str] | None = None,
    tests_added: int = 1,
    tests_passing: int = 1,
    warnings: list[str] | None = None,
) -> SubagentResult:
    transcript_lines = [
        f"VERDICT: {verdict}",
        f"SUMMARY: {summary or f'{step} complete via {engine_name}'}",
        "FILES_CHANGED:",
    ]
    for path in files_changed or []:
        transcript_lines.append(f"- {path}")
    transcript_lines.extend(
        [
            f"TESTS_ADDED: {tests_added}",
            f"TESTS_PASSING: {tests_passing}",
            "WARNINGS:",
        ]
    )
    for warning in warnings or []:
        transcript_lines.append(f"- {warning}")
    transcript = "\n".join(transcript_lines)
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
    return CLIExecutionResult(
        adapter=adapter,
        argv=(adapter, "exec"),
        cwd=tmp_path,
        exit_code=0,
        stdout=(
            "VERDICT: PASS\n"
            f"SUMMARY: {step} complete via {adapter}\n"
            "FILES_CHANGED:\n"
            "- app.txt\n"
            "TESTS_ADDED: 1\n"
            "TESTS_PASSING: 1\n"
            "WARNINGS:\n"
        ),
        stderr="",
    )


__all__ = [name for name in globals() if not name.startswith("__")]
