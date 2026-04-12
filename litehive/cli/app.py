"""CLI entrypoint for litehive."""

from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import click
import typer

from litehive.agents import ENGINE_CHOICES
from litehive.cli.parse import TASK_TYPE_CHOICES
from litehive.cli.backup import (
    cmd_backup_create as backup_create_handler,
    cmd_backup_list as backup_list_handler,
    cmd_backup_restore as backup_restore_handler,
)
from litehive.cli.db import cmd_db_migrate as db_migrate_handler, cmd_db_status as db_status_handler
from litehive.cli.debug import cmd_debug as debug_handler
from litehive.cli.doctor import cmd_doctor as doctor_handler
from litehive.cli.daemon import (
    cmd_daemon_instances as daemon_instances_handler,
    cmd_daemon_restart as daemon_restart_handler,
    cmd_daemon_run as daemon_run_handler,
    cmd_daemon_status as daemon_status_handler,
    cmd_daemon_stop as daemon_stop_handler,
    cmd_daemon_worker as daemon_worker_handler,
)
from litehive.cli.engine import cmd_engine as engine_handler
from litehive.cli.github_import import (
    cmd_import_github as import_github_handler,
)
from litehive.cli.health import cmd_health as health_handler
from litehive.cli.logs import cmd_logs as logs_handler
from litehive.cli.queue import (
    cmd_abandon_task as abandon_task_handler,
    cmd_archive as archive_handler,
    cmd_cleanup as cleanup_handler,
    cmd_close_task as close_task_handler,
    cmd_dirty_worktree_gate as dirty_worktree_gate_handler,
    cmd_move as move_handler,
    cmd_prioritize as prioritize_handler,
    cmd_promote as promote_handler,
    cmd_queue_requeue as queue_requeue_handler,
    cmd_recover as recover_handler,
    cmd_resume_task as resume_task_handler,
    cmd_rollback as rollback_handler,
    cmd_stop_task as stop_task_handler,
    cmd_switch_task as switch_task_handler,
)
from litehive.cli.agent_cli import agent_app
from litehive.cli.report import cmd_report as report_handler
from litehive.cli.run import cmd_run as run_handler
from litehive.cli.status import (
    cmd_list as list_tasks_handler,
    cmd_queue as queue_handler,
    cmd_repair as repair_handler,
    cmd_show as show_task_handler,
    cmd_status as status_handler,
)
from litehive.cli.tasks import (
    cmd_add as add_task_handler,
    cmd_intake as intake_task_handler,
    cmd_issue as issue_task_handler,
    cmd_update as update_task_handler,
)
from litehive.cli.worktree import (
    cmd_worktree_clean as worktree_clean_handler,
    cmd_worktree_ls as worktree_ls_handler,
    cmd_worktree_rescue as worktree_rescue_handler,
)
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
    return status_handler(SimpleNamespace(workspace=Path.cwd(), fast=False, full=False))


@app.command("status", help="Show workspace status")
def status_command(
    workspace: WorkspaceOption = Path.cwd(),
    full: Annotated[bool, typer.Option(help="Include the full per-task status dump.")] = False,
    fast: Annotated[
        bool, typer.Option(help="Deprecated compatibility alias; fast status is now the default")
    ] = False,
) -> int:
    return status_handler(SimpleNamespace(workspace=workspace, fast=fast, full=full))


@app.command("doctor", help="Run workspace integrity checks and optional safe fixes")
def doctor_command(
    workspace: WorkspaceOption = Path.cwd(),
    fix: Annotated[
        bool, typer.Option("--fix", help="Apply deterministic non-destructive fixes where available")
    ] = False,
) -> int:
    return doctor_handler(SimpleNamespace(workspace=workspace, fix=fix))


@app.command("health", help="Show workspace health diagnostics")
def health_command(workspace: WorkspaceOption = Path.cwd()) -> int:
    return health_handler(SimpleNamespace(workspace=workspace))


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
    return engine_handler(
        SimpleNamespace(
            workspace=workspace,
            engine_action=engine_action,
            engine_name=engine_name,
            until=until,
            reason=reason,
        )
    )


@queue_app.callback()
def queue_group(ctx: typer.Context, workspace: WorkspaceOption = Path.cwd()) -> int | None:
    if ctx.invoked_subcommand is not None:
        return None
    return queue_handler(SimpleNamespace(workspace=workspace))


@app.command("repair", help="Repair stale active tasks, interrupted runs, and queue inconsistencies")
def repair_command(workspace: WorkspaceOption = Path.cwd()) -> int:
    return repair_handler(SimpleNamespace(workspace=workspace))


@daemon_app.callback()
def daemon_group(ctx: typer.Context) -> None:
    _require_subcommand(ctx)


@app.command("start", help="Start the background Litehive runner")
def start(workspace: WorkspaceOption = Path.cwd()) -> int:
    return daemon_run_handler(SimpleNamespace(workspace=workspace, foreground=False))


@app.command("stop", help="Stop the background Litehive runner")
def stop_daemon(workspace: WorkspaceOption = Path.cwd()) -> int:
    return daemon_stop_handler(SimpleNamespace(workspace=workspace))


@app.command("restart", help="Restart the background Litehive runner")
def restart(workspace: WorkspaceOption = Path.cwd()) -> int:
    return daemon_restart_handler(SimpleNamespace(workspace=workspace))


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
    return run_handler(
        SimpleNamespace(
            workspace=workspace,
            dry_run=dry_run,
            drain=drain,
            engine=engine,
            model=model,
            max_tasks=max_tasks,
            stop_on_failure=stop_on_failure,
            stop_on_dirty_git=stop_on_dirty_git,
        )
    )


@app.command("dirty-worktree-gate", help="Report whether dirty git state should block the workspace and explain ownership", hidden=True)
def dirty_worktree_gate(workspace: WorkspaceOption = Path.cwd()) -> int:
    return dirty_worktree_gate_handler(SimpleNamespace(workspace=workspace))


@app.command("rollback", help="Revert a task checkpoint commit and requeue the task")
def rollback(
    task_id: Annotated[str, typer.Argument(help="Task id to roll back")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return rollback_handler(SimpleNamespace(task_id=task_id, workspace=workspace))


@app.command("recover", help="Requeue a completed task without reverting code", hidden=True)
def recover(
    task_id: Annotated[str, typer.Argument(help="Task id to recover")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return recover_handler(SimpleNamespace(task_id=task_id, workspace=workspace))


@task_app.command("add", help="Create a queued task")
def add(
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
    return add_task_handler(
        SimpleNamespace(
            workspace=workspace,
            title=title,
            goal=goal,
            acceptance_criteria=acceptance_criteria,
            depends_on=depends_on,
            task_type=task_type,
            mode=mode,
            priority=priority,
        )
    )


@import_app.command("issue", help="File an upstream Litehive issue/task from the current project")
def issue(
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
    return issue_task_handler(
        SimpleNamespace(
            workspace=workspace,
            upstream=upstream,
            type=type,
            details=details,
            acceptance_criteria=acceptance_criteria,
            source_task=source_task,
            source_stage=source_stage,
            source_role=source_role,
            source_project=source_project,
            litehive_workspace=litehive_workspace,
            patch_branch=patch_branch,
            patch_base=patch_base,
            prepare_patch_branch=prepare_patch_branch,
        )
    )


@import_app.command("spec", help="Create a rough task from a freeform spec using an LLM")
def intake(
    file: Annotated[
        Path | None, typer.Argument(help="File containing the brain dump; omit to read from stdin")
    ] = None,
    workspace: WorkspaceOption = Path.cwd(),
    engine: Annotated[
        str, typer.Option(click_type=_choice(ENGINE_CHOICES), help="Engine to use for analysis")
    ] = "opencode",
    model: Annotated[str | None, typer.Option(help="Model override for the selected engine")] = None,
) -> int:
    return intake_task_handler(
        SimpleNamespace(file=file, workspace=workspace, engine=engine, model=model)
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
    return report_handler(
        SimpleNamespace(
            workspace=workspace,
            verdict=verdict,
            message=message,
            role=role,
            step=step,
            task_id=task_id,
        )
    )


@task_app.command("debug", help="Inspect subagent artifacts for a task")
def debug_command(
    task_id: Annotated[str, typer.Argument(help="Task ID (e.g. T-0001)")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    all_: Annotated[
        bool, typer.Option("--all", help="List all subagents with their status and exit code")
    ] = False,
    worktree: Annotated[
        bool, typer.Option(help="Show whether the task worktree exists plus uncommitted and committed changes")
    ] = False,
) -> int:
    return debug_handler(
        SimpleNamespace(task_id=task_id, workspace=workspace, all=all_, worktree=worktree)
    )


@task_app.command("logs", help="Show daemon, task journal, and subagent logs")
def logs_command(
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
    return logs_handler(
        SimpleNamespace(
            task_id=task_id,
            workspace=workspace,
            daemon=daemon,
            agent=agent,
            all=all_,
            follow=follow,
        )
    )


@worktree_app.callback()
def worktree_group(ctx: typer.Context) -> None:
    _require_subcommand(ctx)


@task_app.command("list", help="Compact task listing with optional filters")
def list_tasks_command(
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
    return list_tasks_handler(
        SimpleNamespace(
            workspace=workspace,
            show_all=show_all,
            filter_status=filter_status,
            filter_pipeline_status=filter_pipeline_status,
            filter_engine=filter_engine,
        )
    )


@task_app.command("show", help="Print full details for a single task")
def show(
    task_id: Annotated[str, typer.Argument(help="Task ID (e.g. T-0001)")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return show_task_handler(SimpleNamespace(task_id=task_id, workspace=workspace))


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
    return archive_handler(SimpleNamespace(task_id=task_id, workspace=workspace, all_done=all_done))


@backup_app.callback()
def backup_group(ctx: typer.Context) -> None:
    _require_subcommand(ctx)


@queue_app.command("move", help="Move a queued task to a 1-based position")
@queue_app.command("move", help="Move a queued task to a 1-based position")
def move_command(
    task_id: Annotated[str, typer.Argument(help="Queued task id to move")] = ...,
    position: Annotated[int, typer.Argument(help="Target queue position (1-based)")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return move_handler(SimpleNamespace(task_id=task_id, position=position, workspace=workspace))


@app.command("prioritize", help="Move multiple queued tasks to the front in the requested order", hidden=True)
def prioritize(
    task_ids: Annotated[list[str], typer.Argument(help="Queued task ids to move to the front, in the requested order")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return prioritize_handler(SimpleNamespace(task_ids=task_ids, workspace=workspace))


@queue_app.command("promote", help="Move a queued task to the front of the queue")
def promote_command(
    task_id: Annotated[str, typer.Argument(help="Queued task id to promote")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return promote_handler(SimpleNamespace(task_id=task_id, workspace=workspace))


@queue_app.command("requeue", help="Requeue a flagged or closed task")
def queue_requeue(
    task_id: Annotated[str, typer.Argument(help="Task id to requeue")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    front: Annotated[bool, typer.Option(help="Insert the task at the front of the queue")] = False,
    force: Annotated[
        bool, typer.Option(help="Force requeue even if the task has been flagged 3+ times")
    ] = False,
) -> int:
    return queue_requeue_handler(
        SimpleNamespace(task_id=task_id, workspace=workspace, front=front, force=force)
    )


@queue_app.command("resume", help="Resume an interrupted, parked, flagged, or closed task from its current stage")
def resume_command(
    task_id: Annotated[str, typer.Argument(help="Task id to resume")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    front: Annotated[bool, typer.Option(help="Insert the task at the front of the queue")] = False,
) -> int:
    return resume_task_handler(SimpleNamespace(task_id=task_id, workspace=workspace, front=front))


@task_app.command("abandon", help="Cancel a flagged or closed task and remove it from the queue")
def abandon_command(
    task_id: Annotated[str, typer.Argument(help="Task id to abandon")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return abandon_task_handler(SimpleNamespace(task_id=task_id, workspace=workspace))


@queue_app.command("stop", help="Stop the current active task cleanly")
def queue_stop(workspace: WorkspaceOption = Path.cwd()) -> int:
    return stop_task_handler(SimpleNamespace(workspace=workspace))


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
    return switch_task_handler(
        SimpleNamespace(task_id=task_id, engine=engine, workspace=workspace, reason=reason)
    )


@task_app.command("close", help="Close a task with an explicit non-implementation outcome (wont_do, deferred, duplicate)")
def close_command(
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
    return close_task_handler(
        SimpleNamespace(
            task_id=task_id,
            workspace=workspace,
            outcome=outcome,
            reason=reason,
            follow_up_task=follow_up_task,
        )
    )


@task_app.command("update", help="Update task metadata")
def update(
    task_id: Annotated[str, typer.Argument(help="Task id to update")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
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
    return update_task_handler(
        SimpleNamespace(
            task_id=task_id,
            workspace=workspace,
            priority=priority,
            goal=goal,
            depends_on=depends_on,
            acceptance_criteria=acceptance_criteria,
            constraint=constraint,
            plan_step=plan_step,
            from_file=from_file,
            edit=edit,
        )
    )


@daemon_app.command("run", help="Start the workspace daemon")
def daemon_run(
    workspace: WorkspaceOption = Path.cwd(),
    foreground: Annotated[bool, typer.Option(hidden=True)] = False,
) -> int:
    return daemon_run_handler(SimpleNamespace(workspace=workspace, foreground=foreground))


@daemon_app.command("status", help="Show daemon state for a workspace")
def daemon_status(workspace: WorkspaceOption = Path.cwd()) -> int:
    return daemon_status_handler(SimpleNamespace(workspace=workspace))


@daemon_app.command("stop", help="Stop the workspace daemon")
def daemon_stop(workspace: WorkspaceOption = Path.cwd()) -> int:
    return daemon_stop_handler(SimpleNamespace(workspace=workspace))


@daemon_app.command("restart", help="Restart the workspace daemon")
def daemon_restart(workspace: WorkspaceOption = Path.cwd()) -> int:
    return daemon_restart_handler(SimpleNamespace(workspace=workspace))


@daemon_app.command("instances", help="List all live Litehive daemons")
def daemon_instances() -> int:
    return daemon_instances_handler(SimpleNamespace())


@daemon_app.command("worker", hidden=True)
def daemon_worker(workspace: WorkspaceOption = Path.cwd()) -> int:
    return daemon_worker_handler(SimpleNamespace(workspace=workspace))


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
    return import_github_handler(
        SimpleNamespace(workspace=workspace, issue_ref=issue_ref, repo=repo, all=all_)
    )


@archive_app.command("cleanup", help="Delete archived tasks older than a given duration")
def cleanup_command(
    workspace: WorkspaceOption = Path.cwd(),
    older_than: Annotated[
        str, typer.Option(help="Duration threshold (e.g. 30d, 24h, 60m)")
    ] = ...,
) -> int:
    return cleanup_handler(SimpleNamespace(workspace=workspace, older_than=older_than))


@backup_app.command("create", help="Create a compressed backup of the workspace runtime database")
def backup_create(workspace: WorkspaceOption = Path.cwd()) -> int:
    return backup_create_handler(SimpleNamespace(workspace=workspace))


@backup_app.command("list", help="List available workspace runtime database backups")
def backup_list(workspace: WorkspaceOption = Path.cwd()) -> int:
    return backup_list_handler(SimpleNamespace(workspace=workspace))


@backup_app.command("restore", help="Restore a workspace runtime database backup")
def backup_restore(
    timestamp: Annotated[str, typer.Argument(help="Backup timestamp shown by `litehive backup list`")] = ...,
    workspace: WorkspaceOption = Path.cwd(),
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip the overwrite confirmation prompt")
    ] = False,
) -> int:
    return backup_restore_handler(SimpleNamespace(timestamp=timestamp, workspace=workspace, yes=yes))


@db_app.callback()
def db_group(ctx: typer.Context, workspace: WorkspaceOption = Path.cwd()) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _require_subcommand(ctx)


@db_app.command("status", help="Show workspace database schema version and pending migrations")
def db_status(workspace: WorkspaceOption = Path.cwd()) -> int:
    return db_status_handler(SimpleNamespace(workspace=workspace))


@db_app.command("migrate", help="Apply pending workspace database migrations")
def db_migrate(
    workspace: WorkspaceOption = Path.cwd(),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show pending migrations without applying them")
    ] = False,
) -> int:
    return db_migrate_handler(SimpleNamespace(workspace=workspace, dry_run=dry_run))


@worktree_app.command("ls", help="List Litehive-managed task worktrees with task status and change count")
def worktree_ls(workspace: WorkspaceOption = Path.cwd()) -> int:
    return worktree_ls_handler(SimpleNamespace(workspace=workspace))


@worktree_app.command("clean", help="Remove Litehive-managed worktrees for closed tasks")
def worktree_clean(
    workspace: WorkspaceOption = Path.cwd(),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show which worktrees would be removed without removing them")
    ] = False,
) -> int:
    return worktree_clean_handler(SimpleNamespace(workspace=workspace, dry_run=dry_run))


@worktree_app.command("rescue", help="List or rescue merge-failed worktree commits onto main")
def worktree_rescue(
    workspace: WorkspaceOption = Path.cwd(),
    apply: Annotated[
        bool, typer.Option(help="Cherry-pick eligible worktree commits onto main")
    ] = False,
) -> int:
    return worktree_rescue_handler(SimpleNamespace(workspace=workspace, apply=apply))


app.add_typer(queue_app, name="queue", help="Show the active task and queued order")
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
