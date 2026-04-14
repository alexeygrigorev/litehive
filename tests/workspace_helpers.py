import argparse
import gzip
import os
from pathlib import Path, PurePosixPath
import shutil as _shutil
import signal
import subprocess
import sys
import tempfile as _tempfile
import threading
import time
import types

import pytest
import yaml
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litehive.cli.app import app as cli_app
from litehive.config.loading import load_config
from litehive.config.model import LitehiveConfig
from litehive.config.paths import global_config_path, state_path, worktree_root
from litehive.config.workspace import ensure_workspace
from litehive.config.dataclasses import ExternalEngineSandboxConfig, ExternalEngineSandboxPolicy, SandboxCredentialInput
from litehive.config.profiles.loader import available_process_profiles, resolve_process_profile
from litehive.config.profiles.rendering import render_context_template
from heru import (
    extract_engine_continuation,
    extract_engine_timeline,
    get_engine,
)
from heru.adapters import (
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
)
from heru.base import (
    AdapterCapabilities,
    CLIExecutionResult,
    ExternalCLIAdapter,
)
from litehive.agents.sandbox import SandboxLauncher
from litehive.git.ops import GitError, checkpoint_message, commit_task
from heru.types import SubagentRef
from litehive.domain.engine import (
    EngineUsageObservation,
    EngineUsageWindow,
    LiveEvent,
    LiveTimeline,
)
from litehive.domain.reports import FollowUpTaskSpec, StageReport
from litehive.domain.runtime import (
    ResourceLimitEvent,
    RuntimeContinuationHandoff,
    RuntimeEngineContinuation,
    RuntimeInterruptionState,
    RuntimeStageState,
    RuntimeSubagentState,
    RunnerStatusState,
)
from litehive.domain.task import TaskRecord
from litehive.observability.engine_monitoring import load_engine_monitoring, record_engine_execution
from litehive.observability.status import render_task_summary
from litehive.domain.pool import TaskPoolStopConditions
from litehive.recovery.execution_recovery import recover_completed_task
from litehive.config.engine_models import (
    resolve_engine_name,
    resolve_engine_plan,
    resolve_model,
)
from litehive.lifecycle.orchestration import run_task, ExecutionResult
from litehive.tasks.queue import dequeue_next_task
from litehive.agents.manager import SubagentManager
from litehive.domain.agent import EngineFailure, SubagentResult
from litehive.agents.parsing import stage_report_from_subagent
from litehive.agents.prompts import stage_prompt
from litehive.tasks.archive import (
    archive_done_tasks,
    archive_root,
    archive_task,
    cleanup_archived_tasks,
    list_archived_tasks,
)
from litehive.state.records import (
    create_follow_up_tasks,
    create_task,
    get_task,
    get_task_worktree_path,
    list_tasks,
    require_task,
    save_task,
    save_task_runtime,
)
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.tasks.normalization import (
    implementation_entry_stage,
    needs_normalization,
    reroute_stage_for_acceptance_criteria,
    task_requires_acceptance_criteria,
)
from litehive.tasks.paths import task_dir, task_file, task_runtime_file
from litehive.tasks.persistence import load_state, save_state
from litehive.tasks.queue import move_queued_task
from litehive.tasks.queue import (
    dequeue_next_task_selection,
    peek_next_task_selection,
    restore_untouched_active_task,
    set_active_task,
)
from litehive.tasks.reports import append_thread_comment, load_task_thread
from litehive.state.locking import runner_heartbeat, runner_status, workspace_runner_guard
from litehive.recovery.workspace_repair import (
    mark_interrupted_subagent,
    prepare_interrupted_task,
    recover_stale_runner_state,
)
from litehive.tasks.runtime import (
    finish_task_run_transition,
    mark_subagent_started,
    mark_task_run_started,
)
from litehive.tasks.status import (
    abandon_task,
    close_task,
    requeue_task,
    resume_task,
    stop_current_task,
    switch_task_engine,
    update_task,
    update_task_metadata,
)
import litehive.tasks.persistence as _tasks_persistence
import litehive.state.locking as _workspace_locking
import litehive.state.persist as _workspace_workflow

_runner = CliRunner()


def _invoke_cli(argv, *, input_text: str | None = None):
    normalized = [str(arg) for arg in argv if arg is not None]
    result = _runner.invoke(cli_app, normalized, input=input_text, standalone_mode=False)
    if result.output:
        print(result.output, end="")
    if result.exit_code != 0:
        return result.exit_code
    if isinstance(result.return_value, int):
        return result.return_value
    return 0


def _cmd_abandon_task(args):
    return _invoke_cli(["task", "abandon", args.task_id, "--workspace", args.workspace])


def _cmd_add(args):
    argv = ["task", "add", args.title, "--workspace", args.workspace, "--goal", args.goal]
    for criterion in args.acceptance_criteria or []:
        argv.extend(["--acceptance-criteria", criterion])
    for dep in args.depends_on or []:
        argv.extend(["--depends-on", dep])
    if args.task_type is not None:
        argv.extend(["--task-type", args.task_type])
    if args.priority is not None:
        argv.extend(["--priority", args.priority])
    return _invoke_cli(argv)


def _cmd_archive(args):
    ensure_workspace(args.workspace)
    try:
        if getattr(args, "all_done", False):
            tasks = archive_done_tasks(
                args.workspace,
                on_skip=lambda skipped_task_id, exc: print(
                    f"archive skipped: {skipped_task_id} ({exc})"
                ),
            )
            for task in tasks:
                print(f"archived: {task.id} {task.title}")
            print(f"archived_count: {len(tasks)}")
        else:
            task = archive_task(args.workspace, args.task_id)
            print(f"archived: {task.id} {task.title}")
            print("archived_count: 1")
    except ValueError as exc:
        print(f"archive failed: {exc}")
        return 1
    return 0


def _cmd_cleanup(args):
    return _invoke_cli(["archive", "cleanup", "--workspace", args.workspace, "--older-than", args.older_than])


def _cmd_close_task(args):
    argv = ["task", "close", args.task_id, "--workspace", args.workspace, "--outcome", args.outcome]
    if getattr(args, "reason", None) is not None:
        argv.extend(["--reason", args.reason])
    if getattr(args, "follow_up_task", None) is not None:
        argv.extend(["--follow-up-task", args.follow_up_task])
    return _invoke_cli(argv)


def _cmd_debug(args):
    argv = ["task", "debug", args.task_id, "--workspace", args.workspace]
    if getattr(args, "all", False):
        argv.append("--all")
    if getattr(args, "worktree", False):
        argv.append("--worktree")
    return _invoke_cli(argv)


def _cmd_health(args):
    return _invoke_cli(["health", "--workspace", args.workspace])


def _cmd_list(args):
    argv = ["task", "list", "--workspace", args.workspace]
    if getattr(args, "show_all", False):
        argv.append("--all")
    if getattr(args, "filter_status", None) is not None:
        argv.extend(["--status", args.filter_status])
    if getattr(args, "filter_pipeline_status", None) is not None:
        argv.extend(["--pipeline-status", args.filter_pipeline_status])
    if getattr(args, "filter_engine", None) is not None:
        argv.extend(["--engine", args.filter_engine])
    return _invoke_cli(argv)


def _cmd_logs(args):
    argv = ["task", "logs", "--workspace", args.workspace]
    if getattr(args, "task_id", None) is not None:
        argv.insert(2, args.task_id)
    if getattr(args, "daemon", False):
        argv.append("--daemon")
    if getattr(args, "agent", False):
        argv.append("--agent")
    if getattr(args, "all", False):
        argv.append("--all")
    if getattr(args, "follow", False):
        argv.append("--follow")
    return _invoke_cli(argv)


def _cmd_move(args):
    return _invoke_cli(["queue", "move", args.task_id, args.position, "--workspace", args.workspace])


def _cmd_prioritize(args):
    return _invoke_cli(["prioritize", *args.task_ids, "--workspace", args.workspace])


def _cmd_promote(args):
    return _invoke_cli(["queue", "promote", args.task_id, "--workspace", args.workspace])


def _cmd_queue(args):
    return _invoke_cli(["queue", "--workspace", args.workspace])


def _cmd_recover(args):
    return _invoke_cli(["recover", args.task_id, "--workspace", args.workspace])


def _cmd_repair(args):
    return _invoke_cli(["repair", "--workspace", args.workspace])


def _cmd_report(args):
    argv = ["report", "--verdict", args.verdict, "--message", args.message]
    if args.workspace is not None:
        argv.extend(["--workspace", args.workspace])
    for name in ["role", "step", "task_id"]:
        value = getattr(args, name, None)
        if value is not None:
            argv.extend([f"--{name.replace('_', '-')}", value])
    for path in getattr(args, "files_changed", None) or []:
        argv.extend(["--files-changed", path])
    return _invoke_cli(argv)


def _cmd_requeue_task(args):
    argv = ["queue", "requeue", args.task_id, "--workspace", args.workspace]
    if getattr(args, "front", False):
        argv.append("--front")
    if getattr(args, "force", False):
        argv.append("--force")
    return _invoke_cli(argv)


def _cmd_resume_task(args):
    argv = ["queue", "resume", args.task_id, "--workspace", args.workspace]
    if getattr(args, "front", False):
        argv.append("--front")
    return _invoke_cli(argv)


def _cmd_run(args):
    argv = ["run", "--workspace", args.workspace]
    if getattr(args, "dry_run", False):
        argv.append("--dry-run")
    if getattr(args, "drain", False):
        argv.append("--drain")
    for name in ["engine", "model", "max_tasks"]:
        value = getattr(args, name, None)
        if value is not None:
            argv.extend([f"--{name.replace('_', '-')}", value])
    if getattr(args, "stop_on_failure", None):
        argv.append("--stop-on-failure")
    if getattr(args, "stop_on_dirty_git", None):
        argv.append("--stop-on-dirty-git")
    return _invoke_cli(argv)


def _cmd_show(args):
    return _invoke_cli(["task", "show", args.task_id, "--workspace", args.workspace])


def _cmd_status(args):
    argv = ["status", "--workspace", args.workspace]
    if getattr(args, "full", False):
        argv.append("--full")
    return _invoke_cli(argv)


def _cmd_stop_task(args):
    return _invoke_cli(["queue", "stop", "--workspace", args.workspace])


def _cmd_switch_task(args):
    return _invoke_cli(["switch", args.task_id, args.engine, "--workspace", args.workspace, "--reason", args.reason])


def _cmd_update(args):
    argv = ["task", "update", args.task_id, "--workspace", args.workspace]
    for name in ["title", "priority", "goal", "from_file"]:
        value = getattr(args, name, None)
        if value is not None:
            argv.extend([f"--{name.replace('_', '-')}", value])
    for option_name, values in [
        ("depends-on", getattr(args, "depends_on", None)),
        ("acceptance-criteria", getattr(args, "acceptance_criteria", None)),
        ("constraint", getattr(args, "constraint", None)),
        ("plan-step", getattr(args, "plan_step", None)),
    ]:
        for value in values or []:
            argv.extend([f"--{option_name}", value])
    if getattr(args, "edit", False):
        argv.append("--edit")
    return _invoke_cli(argv)


def _cmd_worktree_clean(args):
    argv = ["worktree", "clean", "--workspace", args.workspace]
    if getattr(args, "dry_run", False):
        argv.append("--dry-run")
    return _invoke_cli(argv)


def _cmd_worktree_ls(args):
    return _invoke_cli(["worktree", "ls", "--workspace", args.workspace])


def _cmd_worktree_rescue(args):
    argv = ["worktree", "rescue", "--workspace", args.workspace]
    if getattr(args, "apply", False):
        argv.append("--apply")
    return _invoke_cli(argv)

tasks_module = types.SimpleNamespace(
    atomic_write_text=_tasks_persistence.atomic_write_text,
    mark_interrupted_subagent=mark_interrupted_subagent,
    merged_state_for_runner_owned_write=_workspace_workflow.merged_state_for_runner_owned_write,
    prepare_interrupted_task=prepare_interrupted_task,
    save_state_without_runner_guard=_tasks_persistence.save_state_without_runner_guard,
    workspace_transition_writes=_workspace_workflow.workspace_transition_writes,
    append_thread_comment=append_thread_comment,
    fcntl=_workspace_locking.fcntl,
    load_task_thread=load_task_thread,
    mark_task_run_started=mark_task_run_started,
    resume_task=resume_task,
    runner_heartbeat=runner_heartbeat,
    signal=signal,
    state_path=state_path,
    task_dir=task_dir,
    update_task=update_task,
    workspace_runner_guard=workspace_runner_guard,
)


def run_next_task(root, **kwargs):
    """v2 replacement for the deleted v1 run_next_task."""
    task = dequeue_next_task(root)
    if task is None:
        return ExecutionResult(task=None, final_state=None, final_stage="none")
    return run_task(root, task, **kwargs)




def _block_runner_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    real_flock = tasks_module.fcntl.flock

    def fake_flock(fd, flags):  # type: ignore[no-untyped-def]
        if flags & tasks_module.fcntl.LOCK_NB:
            raise BlockingIOError("runner is busy")
        return real_flock(fd, flags)

    monkeypatch.setattr("litehive.state.locking.fcntl.flock", fake_flock)


def _fail_atomic_write_on_path(
    monkeypatch: pytest.MonkeyPatch, failing_path: Path, message: str = "write failed"
) -> None:
    original_atomic_write = tasks_module.atomic_write_text

    def fail_on_selected_write(path: Path, content: str) -> None:
        if path == failing_path:
            raise OSError(message)
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks.persistence.atomic_write_text", fail_on_selected_write)


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
    resolved = path.resolve()
    parts = resolved.parts
    for i, part in enumerate(parts):
        if part == ".litehive" and i + 2 < len(parts) and parts[i + 1] == "worktrees":
            # Main workspace root is the parent of .litehive/
            return Path(*parts[:i])
    git_common_dir = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=resolved,
        capture_output=True,
        text=True,
        check=False,
    )
    if git_common_dir.returncode == 0:
        common_dir_text = git_common_dir.stdout.strip()
        if common_dir_text:
            common_dir = Path(common_dir_text).resolve()
            if common_dir.name == ".git":
                return common_dir.parent
    return resolved


def _task_worktree_path(root: Path, task: "TaskRecord") -> Path:
    return worktree_root(root) / f"{task.id}-{task.slug}"


def _write_cli_verdict(
    root: Path,
    task: "TaskRecord",
    step: str,
    verdict: str = "pass",
    message: str | None = None,
    files_changed: list[str] | None = None,
    role: str = "swe",
) -> None:
    """Simulate a litehive report CLI invocation by writing a thread comment.
    """
    from litehive.domain.reports import TaskThreadComment

    # Write to the main workspace root, not the worktree.
    ws_root = _resolve_workspace_root(root)
    task_dir = tasks_module.task_dir(ws_root, task)
    task_dir.mkdir(parents=True, exist_ok=True)

    tasks_module.append_thread_comment(
        ws_root,
        task,
        TaskThreadComment(
            role=role,
            step=step,
            verdict=verdict,
            message=message or f"{step} {verdict}",
            files_changed=list(files_changed or []),
        ),
    )


def _completed_subagent_result(
    tmp_path: Path, step: str, *, engine_name: str = "codex", task: "TaskRecord | None" = None
) -> SubagentResult:
    effective_step = "grooming" if step == "backlog" else step
    worktrees_root = worktree_root(tmp_path)
    if effective_step == "implementing" and worktrees_root.exists():
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
    elif effective_step == "implementing":
        # No worktrees dir — write a change in the main repo so the git-based guard detects it.
        app_file = tmp_path / "app.txt"
        app_file.write_text("implemented\n", encoding="utf-8")

    # Simulate CLI verdict submission via thread comment.
    if task is not None:
        report_role = "planner" if effective_step == "grooming" else "swe"
        _write_cli_verdict(
            tmp_path,
            task,
            effective_step,
            verdict="pass",
            message=f"{step} complete via {engine_name}",
            role=report_role,
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
    from litehive.state.records import get_task as _get_task
    from litehive.tasks.persistence import load_state as _load_state

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
    "ExternalEngineSandboxConfig",
    "ExternalEngineSandboxPolicy",
    "LitehiveConfig",
    "SandboxCredentialInput",
    "available_process_profiles",
    "ensure_workspace",
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
    "load_engine_monitoring",
    "record_engine_execution",
    "render_task_summary",
    "TaskExecutionRunner",
    "TaskPoolStopConditions",
    "_allowed_commit_paths",
    "_commit_to_git_report",
    "_role_for_step",
    "_unexpected_dirty_paths",
    "recover_completed_task",
    "resolve_engine_name",
    "resolve_engine_plan",
    "resolve_model",
    "resolve_next_task",
    "run_next_task",
    "run_single_task",
    "run_task",
    "EngineFailure",
    "SubagentManager",
    "SubagentResult",
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
