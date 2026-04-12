"""CLI entrypoint for litehive."""

import argparse
from pathlib import Path
from typing import Annotated

import click
import typer

from litehive.agents import ENGINE_CHOICES
from litehive.cli.attention import cmd_attention_list, cmd_attention_resolve
from litehive.cli.parse import TASK_TYPE_CHOICES
from litehive.cli.backup import cmd_backup_create, cmd_backup_list, cmd_backup_restore
from litehive.cli.db import cmd_db_migrate, cmd_db_status
from litehive.cli.configure import cmd_configure
from litehive.cli.debug import cmd_debug
from litehive.cli.doctor import cmd_doctor
from litehive.cli.daemon import (
    cmd_daemon_instances,
    cmd_daemon_restart,
    cmd_daemon_run,
    cmd_daemon_status,
    cmd_daemon_stop,
    cmd_daemon_worker,
)
from litehive.cli.engine import cmd_engine
from litehive.cli.github_import import cmd_import_github, cmd_import_issue, cmd_import_issues
from litehive.cli.health import cmd_health
from litehive.cli.logs import cmd_logs
from litehive.cli.parsers import COMMAND_PARSER_BUILDERS
from litehive.cli.queue import (
    cmd_abandon_task,
    cmd_archive,
    cmd_cleanup,
    cmd_close_task,
    cmd_dirty_worktree_gate,
    cmd_move,
    cmd_prioritize,
    cmd_promote,
    cmd_queue_requeue,
    cmd_recover,
    cmd_requeue_task,
    cmd_resume_task,
    cmd_rollback,
    cmd_stop_task,
    cmd_switch_task,
)
from litehive.cli.agent_cli import agent_app
from litehive.cli.report import cmd_report
from litehive.cli.run import cmd_run
from litehive.cli.status import cmd_list, cmd_queue, cmd_repair, cmd_show, cmd_status
from litehive.cli.tasks import cmd_add, cmd_intake, cmd_issue, cmd_update
from litehive.cli.worktree import cmd_worktree_clean, cmd_worktree_ls, cmd_worktree_rescue
from litehive.config import VALID_POOL_SELECTION_POLICIES, available_process_profiles
from litehive.pipeline.orchestration import run_task
from litehive.tasks.queue_ops import dequeue_next_task
from litehive.tasks.constants import VALID_TASK_PRIORITIES

WorkspaceOption = Annotated[
    Path,
    typer.Option("--workspace", help="Repository root containing .litehive/"),
]

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=False,
)
daemon_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=False,
)
task_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=False,
)
queue_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=False,
)
attention_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=False,
)
import_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=False,
)
archive_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=False,
)
backup_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=False,
)
db_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=False,
)
worktree_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=False,
)
pipeline_app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=False,
)


def _choice(values: list[str] | tuple[str, ...] | set[str]) -> click.Choice:
    return click.Choice(sorted(values), case_sensitive=True)


def _ns(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _ctx_command(ctx: typer.Context) -> str:
    return ctx.info_name or ""


def _require_subcommand(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(ctx.get_help())
    raise typer.Exit(2)


def _run_next_task(root: Path):
    """Dequeue the next task and run it through the v2 state machine."""
    task = dequeue_next_task(root)
    if task is None:
        return None
    return run_task(root, task)


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> int | None:
    if ctx.invoked_subcommand is not None:
        return None

    result = _run_next_task(Path.cwd())
    if result is not None and result.task is not None:
        print(f"{result.task.id}: {result.final_stage}")
        return 0
    return cmd_status(_ns(command="status", workspace=Path.cwd(), fast=False, full=False))


@app.command("configure", help="Initialize litehive workspace config")
def configure_command(
    workspace: Annotated[
        Path, typer.Option("--workspace", help="Repository root where .litehive/ should be created")
    ] = Path.cwd(),
    default_engine: Annotated[str, typer.Option(help="Default engine adapter name")] = "codex",
    process_profile: Annotated[
        str,
        typer.Option(
            click_type=_choice(available_process_profiles()),
            help="Prompt/process overlay preset for workspace initialization",
        ),
    ] = "generic",
    default_retry_limit: Annotated[
        int, typer.Option(help="Default retry limit for tasks without a task-specific override")
    ] = 3,
    litehive_source_path: Annotated[
        str | None,
        typer.Option(help="Path to the Litehive source repo/workspace used for upstream issue filing and patch handoff"),
    ] = None,
    opencode_model: Annotated[
        str, typer.Option(help="Default model identifier when using the opencode adapter")
    ] = "zai-coding-plan/glm-5.1",
    gemini_model: Annotated[
        str | None, typer.Option(help="Default model identifier when using the gemini adapter")
    ] = None,
    copilot_model: Annotated[
        str | None, typer.Option(help="Default model identifier when using the copilot adapter")
    ] = None,
    claude_model: Annotated[
        str, typer.Option(help="Default model identifier when using the claude adapter")
    ] = "claude-sonnet-4-20250514",
    claude_max_turns: Annotated[
        int,
        typer.Option(help="Maximum conversation turns per claude invocation (guardrail against accidental quota burn)"),
    ] = 30,
    pool_stop_on_failure: Annotated[
        bool,
        typer.Option("--pool-stop-on-failure", help="Default pool behavior: stop after the first task that does not finish successfully"),
    ] = False,
    pool_max_tasks: Annotated[
        int | None, typer.Option(help="Default pool behavior: stop after completing this many tasks")
    ] = None,
    pool_stop_on_dirty_git: Annotated[
        bool,
        typer.Option("--pool-stop-on-dirty-git", help="Default pool behavior: stop when the git worktree is dirty before starting another task"),
    ] = False,
    pool_stop_on_attention: Annotated[
        bool,
        typer.Option("--pool-stop-on-attention", help="Default pool behavior: stop before starting more work when unresolved attention items exist"),
    ] = False,
    pool_selection_policy: Annotated[
        str,
        typer.Option(
            click_type=_choice(VALID_POOL_SELECTION_POLICIES),
            help="Default pool task ordering policy",
        ),
    ] = "dependency_aware",
    hook: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "Add a runner hook as HOOK_POINT=reject|run:COMMAND. "
                "Supported points: before_grooming, after_grooming, before_implementing, "
                "after_implementing, before_testing, after_testing, before_accepting, "
                "after_accepting, after_commit. "
                "reject_on_failure only valid for after_implementing and after_testing."
            )
        ),
    ] = None,
) -> int:
    return cmd_configure(_ns(**locals()))


@app.command("status", help="Show workspace status")
def status_command(
    workspace: WorkspaceOption = Path.cwd(),
    full: Annotated[bool, typer.Option(help="Include the full per-task status dump.")] = False,
    fast: Annotated[
        bool, typer.Option(help="Deprecated compatibility alias; fast status is now the default")
    ] = False,
) -> int:
    return cmd_status(_ns(command="status", **locals()))


@attention_app.callback()
def attention_group(ctx: typer.Context) -> int | None:
    if ctx.invoked_subcommand is not None:
        return None
    return None


@attention_app.command("list", help="Show unresolved operator-attention items")
def attention_list_command(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_attention_list(
        _ns(command="attention", attention_command="list", workspace=workspace)
    )


@attention_app.command("resolve", help="Resolve one operator-attention item")
def attention_resolve_command(
    attention_id: Annotated[int, typer.Argument(help="Stable attention item id")],
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return cmd_attention_resolve(
        _ns(
            command="attention",
            attention_command="resolve",
            attention_id=attention_id,
            workspace=workspace,
        )
    )


@app.command("doctor", help="Run workspace integrity checks and optional safe fixes")
def doctor_command(
    workspace: WorkspaceOption = Path.cwd(),
    fix: Annotated[
        bool, typer.Option("--fix", help="Apply deterministic non-destructive fixes where available")
    ] = False,
) -> int:
    return cmd_doctor(_ns(command="doctor", **locals()))


@app.command("health", help="Show workspace health diagnostics")
def health_command(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_health(_ns(command="health", workspace=workspace))


@app.command("engine", help="Manage engine freezes and status")
def engine_command(
    workspace: WorkspaceOption = Path.cwd(),
    engine_action: Annotated[
        str,
        typer.Argument(
            click_type=_choice(["freeze", "unfreeze", "status"]),
            help="Subcommand: freeze, unfreeze, status",
        ),
    ] = ...,
    engine_name: Annotated[
        str | None, typer.Argument(click_type=_choice(ENGINE_CHOICES), help="Engine name (required for freeze/unfreeze)")
    ] = None,
    until: Annotated[
        str | None,
        typer.Option(help="Freeze until this ISO date (YYYY-MM-DD)"),
    ] = None,
    reason: Annotated[
        str | None,
        typer.Option(help="Optional operator note echoed in command output"),
    ] = None,
) -> int:
    return cmd_engine(_ns(command="engine", **locals()))


@queue_app.callback()
def queue_group(ctx: typer.Context, workspace: WorkspaceOption = Path.cwd()) -> int | None:
    if ctx.invoked_subcommand is not None:
        return None
    return cmd_queue(_ns(command="queue", workspace=workspace, queue_command=None))


@app.command("repair", help="Repair stale active tasks, interrupted runs, and queue inconsistencies")
def repair_command(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_repair(_ns(command="repair", workspace=workspace))


@daemon_app.callback()
def daemon_group(ctx: typer.Context) -> None:
    _require_subcommand(ctx)


@app.command("start", help="Start the background Litehive runner")
def start(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_daemon_run(_ns(command="start", workspace=workspace, foreground=False))


@app.command("stop", help="Stop the background Litehive runner")
def stop_daemon(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_daemon_stop(_ns(command="stop", workspace=workspace))


@app.command("restart", help="Restart the background Litehive runner")
def restart(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_daemon_restart(_ns(command="restart", workspace=workspace))


@app.command("run", help="Run the next task once")
def run_command(
    workspace: WorkspaceOption = Path.cwd(),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show the planned selection for single-task or drain mode without invoking any agents"),
    ] = False,
    drain: Annotated[
        bool, typer.Option("--drain", help="Drain the task pool until it reaches an explicit stop condition")
    ] = False,
    engine: Annotated[
        str | None, typer.Option(click_type=_choice(ENGINE_CHOICES), help="Override the engine for this run only")
    ] = None,
    model: Annotated[
        str | None, typer.Option(help="Override the model for supported engines for this run only")
    ] = None,
    stop_on_failure: Annotated[
        bool | None,
        typer.Option("--stop-on-failure", flag_value=True, help="Stop the pool after the first task that does not finish successfully"),
    ] = None,
    max_tasks: Annotated[int | None, typer.Option(help="Stop the pool after completing this many tasks")] = None,
    stop_on_dirty_git: Annotated[
        bool | None,
        typer.Option("--stop-on-dirty-git", flag_value=True, help="Stop the pool when the git worktree is dirty before starting another task"),
    ] = None,
) -> int:
    return cmd_run(_ns(command="run", **locals()))


@app.command("dirty-worktree-gate", help="Report whether dirty git state should block the workspace and explain ownership", hidden=True)
def dirty_worktree_gate(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_dirty_worktree_gate(_ns(command="dirty-worktree-gate", workspace=workspace))


@app.command("rollback", help="Revert a task checkpoint commit and requeue the task")
def rollback(
    task_id: Annotated[str, typer.Argument(help="Task id to roll back")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return cmd_rollback(_ns(command="rollback", task_id=task_id, workspace=workspace))


@app.command("recover", help="Requeue a completed task without reverting code", hidden=True)
def recover(
    task_id: Annotated[str, typer.Argument(help="Task id to recover")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return cmd_recover(_ns(command="recover", task_id=task_id, workspace=workspace))


def _add_command(
    command: str,
    workspace: Path,
    title: str,
    goal: str,
    acceptance_criteria: list[str] | None,
    depends_on: list[str] | None,
    task_type: str | None,
    mode: str | None,
    priority: str | None,
) -> int:
    return cmd_add(_ns(**locals()))


@app.command("add", help="Create a queued task", hidden=True)
@task_app.command("add", help="Create a queued task")
def add(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="Task title")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    goal: Annotated[str, typer.Option(help="Task goal text")] = "",
    acceptance_criteria: Annotated[
        list[str] | None,
        typer.Option(help="Add one acceptance criterion; repeat the flag for structured criteria"),
    ] = None,
    depends_on: Annotated[
        list[str] | None,
        typer.Option(help="Add prerequisite task ids; repeat the flag or use a comma-separated list"),
    ] = None,
    task_type: Annotated[
        str | None, typer.Option(click_type=_choice(TASK_TYPE_CHOICES), help="Explicit routing class for this task")
    ] = None,
    mode: Annotated[str | None, typer.Option(click_type=_choice(["full", "single"]), help="Task pipeline mode; defaults to `full`")] = None,
    priority: Annotated[
        str | None, typer.Option(click_type=_choice(VALID_TASK_PRIORITIES), help="Task priority; defaults to medium when omitted")
    ] = None,
) -> int:
    command_name = "task" if ctx.info_name == "add" and ctx.parent and ctx.parent.info_name == "task" else "add"
    return _add_command(command_name, workspace, title, goal, acceptance_criteria, depends_on, task_type, mode, priority)


@app.command("issue", help="File an upstream Litehive issue/task from the current project", hidden=True)
@import_app.command("issue", help="File an upstream Litehive issue/task from the current project")
def issue(
    ctx: typer.Context,
    workspace: WorkspaceOption = Path.cwd(),
    upstream: Annotated[str, typer.Option(help="Upstream Litehive issue title or short summary")] = ...,
    type: Annotated[
        str, typer.Option(click_type=_choice(["runtime_bug", "missing_feature", "config_improvement", "prompt_improvement", "engine_adapter_fix"]), help="Contribution class for the upstream task")
    ] = "runtime_bug",
    details: Annotated[str, typer.Option(help="Long-form details, reproduction notes, or requested change")] = "",
    acceptance_criteria: Annotated[
        list[str] | None,
        typer.Option(help="Add acceptance criteria for the upstream task; repeat for multiple"),
    ] = None,
    source_task: Annotated[
        str | None, typer.Option(help="Originating task id in the current project; defaults to the active task if any")
    ] = None,
    source_stage: Annotated[
        str | None, typer.Option(help="Originating pipeline stage in the current project")
    ] = None,
    source_role: Annotated[str, typer.Option(help="Role filing the upstream task")] = "recovery",
    source_project: Annotated[
        str | None, typer.Option(help="Override the source project name shown in the upstream task")
    ] = None,
    litehive_workspace: Annotated[
        Path | None, typer.Option(help="Override the target Litehive repo/workspace instead of using litehive_source_path")
    ] = None,
    patch_branch: Annotated[
        str | None, typer.Option(help="Branch name in the Litehive repo for a proposed fix handoff")
    ] = None,
    patch_base: Annotated[str, typer.Option(help="Base ref used when preparing --patch-branch (default: HEAD)")] = "HEAD",
    prepare_patch_branch: Annotated[
        bool, typer.Option(help="Create the patch branch in the Litehive repo before filing the task")
    ] = False,
) -> int:
    return cmd_issue(_ns(command=_ctx_command(ctx), **locals()))


@app.command("intake", help="Create a rough task from a freeform brain dump using an LLM", hidden=True)
@import_app.command("spec", help="Create a rough task from a freeform spec using an LLM")
def intake(
    ctx: typer.Context,
    file: Annotated[
        Path | None, typer.Argument(help="File containing the brain dump; omit to read from stdin")
    ] = None,
    workspace: WorkspaceOption = Path.cwd(),
    engine: Annotated[
        str, typer.Option(click_type=_choice(ENGINE_CHOICES), help="Engine to use for analysis")
    ] = "opencode",
    model: Annotated[str | None, typer.Option(help="Model override for the selected engine")] = None,
) -> int:
    return cmd_intake(
        _ns(command=_ctx_command(ctx), file=file, workspace=workspace, engine=engine, model=model)
    )


@app.command("report", help="Submit a stage verdict for the active task")
def report_command(
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Repository root containing .litehive/"),
    ] = None,
    verdict: Annotated[
        str, typer.Option(click_type=_choice(["pass", "fail", "reject", "comment"]))
    ] = ...,
    message: Annotated[
        str, typer.Option(help="Detailed explanation of what was done, what failed, or what needs fixing")
    ] = ...,
    role: Annotated[
        str, typer.Option(help="Role submitting the report (swe, qa, reviewer, planner)")
    ] = "swe",
    step: Annotated[
        str | None,
        typer.Option(help="Stage (grooming, implementing, testing, accepting). Defaults to the task's current pipeline_status."),
    ] = None,
    task_id: Annotated[str | None, typer.Option(help="Task ID. Defaults to the active task.")] = None,
) -> int:
    return cmd_report(_ns(command="report", **locals()))


@app.command("import-issue", help="Import a single GitHub issue as a litehive task", hidden=True)
def import_issue(
    issue_ref: Annotated[
        str, typer.Argument(help="GitHub issue URL (https://github.com/owner/repo/issues/N) or issue number")
    ] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    repo: Annotated[
        str | None, typer.Option(help="GitHub repo as owner/repo; auto-detected from git remote if omitted")
    ] = None,
) -> int:
    return cmd_import_issue(_ns(command="import-issue", issue_ref=issue_ref, workspace=workspace, repo=repo))


@app.command("import-issues", help="Import all open GitHub issues that don't already have litehive tasks", hidden=True)
def import_issues(
    workspace: WorkspaceOption = Path.cwd(),
    repo: Annotated[
        str | None, typer.Option(help="GitHub repo as owner/repo; auto-detected from git remote if omitted")
    ] = None,
) -> int:
    return cmd_import_issues(_ns(command="import-issues", workspace=workspace, repo=repo))


@app.command("debug", help="Inspect subagent artifacts for a task", hidden=True)
@task_app.command("debug", help="Inspect subagent artifacts for a task")
def debug_command(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="Task ID (e.g. T-0001)")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    all_: Annotated[
        bool, typer.Option("--all", help="List all subagents with their status and exit code")
    ] = False,
    worktree: Annotated[
        bool, typer.Option(help="Show whether the task worktree exists plus uncommitted and committed changes")
    ] = False,
) -> int:
    return cmd_debug(
        _ns(command=_ctx_command(ctx), task_id=task_id, workspace=workspace, all=all_, worktree=worktree)
    )


@app.command("logs", help="Show daemon, task journal, and subagent logs", hidden=True)
@task_app.command("logs", help="Show daemon, task journal, and subagent logs")
def logs_command(
    ctx: typer.Context,
    task_id: Annotated[str | None, typer.Argument(help="Optional task ID (e.g. T-0001)")] = None,
    workspace: WorkspaceOption = Path.cwd(),
    daemon: Annotated[bool, typer.Option(help="List the latest daemon run-all sessions")] = False,
    agent: Annotated[
        bool, typer.Option(help="Show subagent transcript/stdout instead of the task journal")
    ] = False,
    all_: Annotated[
        bool, typer.Option("--all", help="List all subagent runs for the task (requires --agent)")
    ] = False,
    follow: Annotated[
        bool, typer.Option(help="Follow the currently running subagent stdout in real time")
    ] = False,
) -> int:
    return cmd_logs(
        _ns(command=_ctx_command(ctx), task_id=task_id, workspace=workspace, daemon=daemon, agent=agent, all=all_, follow=follow)
    )


@worktree_app.callback()
def worktree_group(ctx: typer.Context) -> None:
    _require_subcommand(ctx)


@app.command("list", help="Compact task listing with optional filters", hidden=True)
@task_app.command("list", help="Compact task listing with optional filters")
def list_tasks(
    ctx: typer.Context,
    workspace: WorkspaceOption = Path.cwd(),
    show_all: Annotated[
        bool, typer.Option("--all", help="Include done tasks (excluded by default)")
    ] = False,
    filter_status: Annotated[
        str | None, typer.Option("--status", help="Filter by task status (queued, in_progress, done, ...)")
    ] = None,
    filter_pipeline_status: Annotated[
        str | None, typer.Option("--pipeline-status", help="Filter by pipeline stage (backlog, implementing, ...)")
    ] = None,
    filter_engine: Annotated[
        str | None, typer.Option("--engine", help="Filter by engine name")
    ] = None,
) -> int:
    return cmd_list(
        _ns(command=_ctx_command(ctx), workspace=workspace, show_all=show_all, filter_status=filter_status, filter_pipeline_status=filter_pipeline_status, filter_engine=filter_engine)
    )


@app.command("show", help="Print full details for a single task", hidden=True)
@task_app.command("show", help="Print full details for a single task")
def show(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="Task ID (e.g. T-0001)")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return cmd_show(_ns(command=_ctx_command(ctx), task_id=task_id, workspace=workspace))


@archive_app.callback()
def archive_group(
    ctx: typer.Context,
    task_id: Annotated[str | None, typer.Argument(help="Task ID to archive")] = None,
    workspace: WorkspaceOption = Path.cwd(),
    all_done: Annotated[
        bool, typer.Option("--all-done", help="Archive all done tasks and skip missing or broken task references")
    ] = False,
) -> int | None:
    if ctx.invoked_subcommand is not None:
        return None
    if task_id is None and not all_done:
        typer.echo(ctx.get_help())
        raise typer.Exit(2)
    return cmd_archive(_ns(command="archive", task_id=task_id, workspace=workspace, all_done=all_done))


@backup_app.callback()
def backup_group(ctx: typer.Context) -> None:
    _require_subcommand(ctx)


@queue_app.command("move", help="Move a queued task to a 1-based position")
@app.command("move", help="Move a queued task to a 1-based position", hidden=True)
def move(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="Queued task id to move")] = ...,
    position: Annotated[int, typer.Argument(help="Target queue position (1-based)")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return cmd_move(
        _ns(command=_ctx_command(ctx), task_id=task_id, position=position, workspace=workspace)
    )


@app.command("prioritize", help="Move multiple queued tasks to the front in the requested order", hidden=True)
def prioritize(
    task_ids: Annotated[list[str], typer.Argument(help="Queued task ids to move to the front, in the requested order")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return cmd_prioritize(_ns(command="prioritize", task_ids=task_ids, workspace=workspace))


@queue_app.command("promote", help="Move a queued task to the front of the queue")
@app.command("promote", help="Move a queued task to the front of the queue", hidden=True)
def promote(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="Queued task id to promote")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return cmd_promote(_ns(command=_ctx_command(ctx), task_id=task_id, workspace=workspace))


@queue_app.command("requeue", help="Requeue a flagged or closed task")
def queue_requeue(
    task_id: Annotated[str, typer.Argument(help="Task id to requeue")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    front: Annotated[bool, typer.Option(help="Insert the task at the front of the queue")] = False,
    force: Annotated[
        bool, typer.Option(help="Force requeue even if the task has been flagged 3+ times")
    ] = False,
) -> int:
    return cmd_queue_requeue(_ns(command="queue", queue_command="requeue", task_id=task_id, workspace=workspace, front=front, force=force))


@app.command("requeue", help="Requeue a flagged or closed task", hidden=True)
def requeue_task(
    task_id: Annotated[str, typer.Argument(help="Task id to requeue")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    front: Annotated[bool, typer.Option(help="Insert the task at the front of the queue")] = False,
    force: Annotated[
        bool, typer.Option(help="Force requeue even if the task has been flagged 3+ times")
    ] = False,
) -> int:
    return cmd_requeue_task(_ns(command="requeue", task_id=task_id, workspace=workspace, front=front, force=force))


@queue_app.command("resume", help="Resume an interrupted, parked, flagged, or closed task from its current stage")
@app.command("resume", help="Resume an interrupted, parked, flagged, or closed task from its current stage", hidden=True)
def resume(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="Task id to resume")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    front: Annotated[bool, typer.Option(help="Insert the task at the front of the queue")] = False,
) -> int:
    return cmd_resume_task(
        _ns(command=_ctx_command(ctx), task_id=task_id, workspace=workspace, front=front)
    )


@app.command("abandon", help="Cancel a flagged or closed task and remove it from the queue", hidden=True)
@task_app.command("abandon", help="Cancel a flagged or closed task and remove it from the queue")
def abandon(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="Task id to abandon")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return cmd_abandon_task(
        _ns(command=_ctx_command(ctx), task_id=task_id, workspace=workspace)
    )


@queue_app.command("stop", help="Stop the current active task cleanly")
def queue_stop(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_stop_task(_ns(command="queue", queue_command="stop", workspace=workspace))


@app.command("switch", help="Stop or resume a task, record an engine switch request, and queue it for the next iteration", hidden=True)
def switch(
    task_id: Annotated[str, typer.Argument(help="Task id to switch")] = ...,
    engine: Annotated[
        str, typer.Argument(click_type=_choice(ENGINE_CHOICES), help="Engine to switch to for the next pass")
    ] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    reason: Annotated[
        str, typer.Option(help="Why the engine switch happened; recorded in the task thread comment")
    ] = ...,
) -> int:
    return cmd_switch_task(_ns(command="switch", task_id=task_id, engine=engine, workspace=workspace, reason=reason))


@app.command("close", help="Close a task with an explicit non-implementation outcome (wont_do, deferred, duplicate)", hidden=True)
@task_app.command("close", help="Close a task with an explicit non-implementation outcome (wont_do, deferred, duplicate)")
def close(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="Task id to close")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    outcome: Annotated[
        str, typer.Option(click_type=_choice(["wont_do", "deferred", "duplicate"]), help="Reason the task is being closed without implementation")
    ] = ...,
    reason: Annotated[
        str | None, typer.Option(help="Optional free-text rationale recorded in the task journal")
    ] = None,
    follow_up_task: Annotated[
        str | None, typer.Option(help="Optional existing task id linked as the follow-up for this close decision")
    ] = None,
) -> int:
    return cmd_close_task(
        _ns(command=_ctx_command(ctx), task_id=task_id, workspace=workspace, outcome=outcome, reason=reason, follow_up_task=follow_up_task)
    )


@app.command("update", help="Update task metadata", hidden=True)
@task_app.command("update", help="Update task metadata")
def update(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="Task id to update")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    title: Annotated[str | None, typer.Option(help="Replace the task title")] = None,
    priority: Annotated[
        str | None, typer.Option(click_type=_choice(VALID_TASK_PRIORITIES), help="Set task priority")
    ] = None,
    goal: Annotated[str | None, typer.Option(help="Replace the task goal text")] = None,
    depends_on: Annotated[
        list[str] | None,
        typer.Option(help="Replace task dependencies; repeat the flag or use a comma-separated list, or use 'none' to clear"),
    ] = None,
    acceptance_criteria: Annotated[
        list[str] | None,
        typer.Option(help="Replace acceptance criteria; repeat the flag or use 'none' to clear"),
    ] = None,
    constraint: Annotated[
        list[str] | None,
        typer.Option(help="Replace constraints; repeat the flag or use 'none' to clear"),
    ] = None,
    plan_step: Annotated[
        list[str] | None,
        typer.Option(help="Replace the task plan; repeat the flag or use 'none' to clear"),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option(help="Load rich task updates from a YAML file mapped onto durable task fields"),
    ] = None,
    edit: Annotated[
        bool, typer.Option(help="Open the current task shaping fields in $VISUAL or $EDITOR and persist the edited YAML")
    ] = False,
) -> int:
    return cmd_update(_ns(command=_ctx_command(ctx), **locals()))


@daemon_app.command("run", help="Start the workspace daemon")
def daemon_run(
    workspace: WorkspaceOption = Path.cwd(),
    foreground: Annotated[bool, typer.Option(hidden=True)] = False,
) -> int:
    return cmd_daemon_run(_ns(command="daemon", daemon_command="run", workspace=workspace, foreground=foreground))


@daemon_app.command("status", help="Show daemon state for a workspace")
def daemon_status(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_daemon_status(_ns(command="daemon", daemon_command="status", workspace=workspace))


@daemon_app.command("stop", help="Stop the workspace daemon")
def daemon_stop(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_daemon_stop(_ns(command="daemon", daemon_command="stop", workspace=workspace))


@daemon_app.command("restart", help="Restart the workspace daemon")
def daemon_restart(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_daemon_restart(_ns(command="daemon", daemon_command="restart", workspace=workspace))


@daemon_app.command("instances", help="List all live Litehive daemons")
def daemon_instances() -> int:
    return cmd_daemon_instances(_ns(command="daemon", daemon_command="instances"))


@daemon_app.command("worker", hidden=True)
def daemon_worker(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_daemon_worker(_ns(command="daemon", daemon_command="worker", workspace=workspace))


@import_app.callback()
def import_group(ctx: typer.Context, workspace: WorkspaceOption = Path.cwd()) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _require_subcommand(ctx)


@import_app.command("github", help="Import GitHub issues as Litehive tasks")
def import_github(
    workspace: WorkspaceOption = Path.cwd(),
    issue_ref: Annotated[
        str | None, typer.Argument(help="GitHub issue URL (https://github.com/owner/repo/issues/N) or issue number")
    ] = None,
    repo: Annotated[
        str | None, typer.Option(help="GitHub repo as owner/repo; auto-detected from git remote if omitted")
    ] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Import all open GitHub issues that do not already have Litehive tasks")
    ] = False,
) -> int:
    return cmd_import_github(_ns(command="import", import_command="github", workspace=workspace, issue_ref=issue_ref, repo=repo, all=all_))


@archive_app.command("cleanup", help="Delete archived tasks older than a given duration")
@app.command("cleanup", help="Delete archived tasks older than a given duration", hidden=True)
def cleanup(
    ctx: typer.Context,
    workspace: WorkspaceOption = Path.cwd(),
    older_than: Annotated[
        str, typer.Option(help="Duration threshold (e.g. 30d, 24h, 60m)")
    ] = ...,
) -> int:
    return cmd_cleanup(
        _ns(command=_ctx_command(ctx), workspace=workspace, older_than=older_than)
    )


@backup_app.command("create", help="Create a compressed backup of the workspace runtime database")
def backup_create(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_backup_create(_ns(command="backup", backup_command="create", workspace=workspace))


@backup_app.command("list", help="List available workspace runtime database backups")
def backup_list(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_backup_list(_ns(command="backup", backup_command="list", workspace=workspace))


@backup_app.command("restore", help="Restore a workspace runtime database backup")
def backup_restore(
    timestamp: Annotated[str, typer.Argument(help="Backup timestamp shown by `litehive backup list`")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip the overwrite confirmation prompt")
    ] = False,
) -> int:
    return cmd_backup_restore(
        _ns(command="backup", backup_command="restore", timestamp=timestamp, workspace=workspace, yes=yes)
    )


@db_app.callback()
def db_group(ctx: typer.Context, workspace: WorkspaceOption = Path.cwd()) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _require_subcommand(ctx)


@db_app.command("status", help="Show workspace database schema version and pending migrations")
def db_status(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_db_status(_ns(command="db", db_command="status", workspace=workspace))


@db_app.command("migrate", help="Apply pending workspace database migrations")
def db_migrate(
    workspace: WorkspaceOption = Path.cwd(),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show pending migrations without applying them")
    ] = False,
) -> int:
    return cmd_db_migrate(_ns(command="db", db_command="migrate", workspace=workspace, dry_run=dry_run))


@worktree_app.command("ls", help="List Litehive-managed task worktrees with task status and change count")
def worktree_ls(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_worktree_ls(_ns(command="worktree", worktree_command="ls", workspace=workspace))


@worktree_app.command("clean", help="Remove Litehive-managed worktrees for closed tasks")
def worktree_clean(
    workspace: WorkspaceOption = Path.cwd(),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show which worktrees would be removed without removing them")
    ] = False,
) -> int:
    return cmd_worktree_clean(_ns(command="worktree", worktree_command="clean", workspace=workspace, dry_run=dry_run))


@worktree_app.command("rescue", help="List or rescue merge-failed worktree commits onto main")
def worktree_rescue(
    workspace: WorkspaceOption = Path.cwd(),
    apply: Annotated[
        bool, typer.Option(help="Cherry-pick eligible worktree commits onto main")
    ] = False,
) -> int:
    return cmd_worktree_rescue(_ns(command="worktree", worktree_command="rescue", workspace=workspace, apply=apply))


app.add_typer(queue_app, name="queue", help="Show the active task and queued order")
app.add_typer(attention_app, name="attention", help="List and resolve operator-attention items")
app.add_typer(task_app, name="task", help="Manage Litehive tasks")
app.add_typer(import_app, name="import", help="Import or file tasks from external inputs")
app.add_typer(archive_app, name="archive", help="Move done tasks to the archive directory")
app.add_typer(backup_app, name="backup", help="Create, list, and restore workspace database backups")
app.add_typer(db_app, name="db", help="Inspect and migrate the workspace database schema")
app.add_typer(worktree_app, name="worktree", help="Inspect and clean Litehive-managed task worktrees")
app.add_typer(daemon_app, name="daemon", help="Manage the Litehive pool daemon", hidden=True)
app.add_typer(pipeline_app, name="pipeline", help="Inspect the v2 pipeline state machine")
app.add_typer(agent_app, name="agent", help="Agent-restricted commands (verdict submission)")


@pipeline_app.command("graph", help="Print a Mermaid stateDiagram-v2 of the v2 pipeline rules")
def pipeline_graph_command(
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Write the Markdown-wrapped diagram to this file instead of stdout",
        ),
    ] = None,
) -> int:
    from litehive.pipeline.diagram import render_markdown

    content = render_markdown()
    if output is None:
        print(content)
    else:
        output.write_text(content)
        print(f"wrote {output}")
    return 0


@pipeline_app.command("rules", help="List the v2 transition rules as readable rows")
def pipeline_rules_command() -> int:
    from litehive.pipeline.transitions import list_transitions

    for rule in list_transitions():
        from_state = (
            "|".join(sorted(rule.from_state))
            if isinstance(rule.from_state, frozenset)
            else rule.from_state
        )
        to = (
            rule.transition_to
            if not callable(rule.transition_to)
            else f"<{rule.transition_to.__name__}>"
        )
        event_name = rule.on_event.__name__
        desc = f"  # {rule.description}" if rule.description else ""
        print(f"{from_state:25s} --[{event_name:25s}]--> {to}{desc}")
    return 0


@pipeline_app.command("set-state", help="Override a task's v2 pipeline stage (operator escape hatch)")
def pipeline_set_state_command(
    task_id: Annotated[str, typer.Argument(help="Task id")],
    stage: Annotated[str, typer.Argument(help="Target stage (e.g. ready, implementing, failed)")],
    workspace: Annotated[
        Path, typer.Option("--workspace", help="Workspace root")
    ] = Path.cwd(),
) -> None:
    from litehive.pipeline.persistence import SqlitePersistence, TaskNotFound

    store = SqlitePersistence(workspace)
    try:
        state = store.load(task_id)
    except TaskNotFound:
        print(f"no v2 state row for {task_id}; use 'litehive pipeline add-task' first")
        raise typer.Exit(1)
    old_stage = state.stage
    state.stage = stage
    store.save(state)
    print(f"task: {task_id}")
    print(f"stage: {old_stage} → {stage}")


@pipeline_app.command("reset", help="Clear all v2 pipeline state for a task so it starts fresh")
def pipeline_reset_command(
    task_id: Annotated[str, typer.Argument(help="Task id")],
    workspace: Annotated[Path, typer.Option("--workspace", help="Workspace root")] = Path.cwd(),
) -> None:
    from litehive.db.schema import connect_workspace_db

    with connect_workspace_db(workspace) as conn:
        for table in ["pipeline_task_state", "pipeline_sessions", "pipeline_transitions", "pipeline_journal"]:
            conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
        conn.commit()
    print(f"task: {task_id}")
    print("reset: ok")


@pipeline_app.command(
    "journal", help="Dump the v2 pipeline journal + transitions for one task"
)
def pipeline_journal_command(
    task_id: Annotated[str, typer.Argument(help="Task id (e.g. T-0001)")],
    workspace: Annotated[
        Path, typer.Option("--workspace", help="Workspace root (.litehive/ container)")
    ] = Path.cwd(),
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="Max transitions to show (most recent)")
    ] = 50,
) -> int:
    from litehive.pipeline.journal import SqliteJournal
    from litehive.pipeline.persistence import SqlitePersistence, TaskNotFound

    journal = SqliteJournal(workspace)
    store = SqlitePersistence(workspace)
    try:
        state = store.load(task_id)
    except TaskNotFound:
        print(f"no v2 state row for task {task_id}")
        raise typer.Exit(1)

    print(f"task: {task_id}")
    print(f"stage: {state.stage}")
    if state.origin_stage:
        print(f"origin_stage: {state.origin_stage}")
    if state.recovery_attempt:
        print(f"recovery_attempt: {dict(state.recovery_attempt)}")
    if state.stage_retry:
        print(f"stage_retry: {dict(state.stage_retry)}")
    if state.failed_reason:
        print(f"failed_reason: {state.failed_reason}")
    if state.failed_message:
        print(f"failed_message: {state.failed_message}")
    if state.last_rejection_by_stage:
        print("last_rejection_by_stage:")
        for stage, rej in state.last_rejection_by_stage.items():
            print(f"  {stage}: source={rej.source} reason={rej.reason}")

    lifecycle = journal.load_lifecycle(task_id)
    if lifecycle:
        print("\nlifecycle:")
        for row in lifecycle:
            print(f"  {row['seq']:3d} {row['created_at']}  {row['kind']}  {row['payload']}")

    transitions = journal.load_transitions(task_id)
    if transitions:
        recent = transitions[-limit:]
        print(f"\ntransitions (last {len(recent)} of {len(transitions)}):")
        for row in recent:
            desc = row["rule_description"] or ""
            print(
                f"  {row['seq']:3d} {row['created_at']}  "
                f"{row['from_stage']:25s} --[{row['event_type']:25s}]--> "
                f"{row['to_stage']}"
                + (f"  # {desc}" if desc else "")
            )
    return 0


_PUBLIC_TOP_LEVEL_COMMANDS = {
    "attention",
    "configure",
    "status",
    "doctor",
    "health",
    "engine",
    "queue",
    "task",
    "import",
    "repair",
    "start",
    "stop",
    "restart",
    "run",
    "rollback",
    "report",
    "worktree",
    "archive",
    "backup",
    "db",
}

_PUBLIC_GROUP_COMMANDS = {
    "attention": {"list", "resolve"},
    "task": {"add", "list", "show", "update", "close", "abandon", "debug", "logs"},
    "queue": {"move", "promote", "requeue", "resume", "stop"},
    "import": {"github", "issue", "spec"},
    "archive": {"cleanup"},
    "backup": {"create", "list", "restore"},
    "db": {"status", "migrate"},
    "worktree": {"ls", "clean", "rescue"},
}


def _subparsers_action(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _filter_public_help(parser: argparse.ArgumentParser, visible: set[str]) -> None:
    action = _subparsers_action(parser)
    if action is None:
        return
    action.metavar = "command"
    action._choices_actions = [
        choice_action
        for choice_action in action._choices_actions
        if choice_action.dest in visible
    ]


def build_parser():
    parser = argparse.ArgumentParser(prog="litehive")
    subparsers = parser.add_subparsers(dest="command")
    for register_parser in COMMAND_PARSER_BUILDERS:
        register_parser(subparsers)
    _filter_public_help(parser, _PUBLIC_TOP_LEVEL_COMMANDS)
    action = _subparsers_action(parser)
    if action is not None:
        for command, visible in _PUBLIC_GROUP_COMMANDS.items():
            child = action.choices.get(command)
            if child is not None:
                _filter_public_help(child, visible)
    return parser


def main() -> int:
    try:
        result = app(standalone_mode=False)
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.Abort:
        return 1
    return 0 if result is None else int(result)
