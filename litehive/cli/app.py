"""CLI entrypoint for litehive."""

from pathlib import Path
import click
import typer

from litehive.cli.agent_cli import agent_app
from litehive.cli.archive_cli import app as archive_app
from litehive.cli.common import make_typer
from litehive.cli.daemon_cli import app as daemon_app
from litehive.cli.import_cli import app as import_app
from litehive.cli.pipeline_cli import app as pipeline_app
from litehive.cli.queue_cli import app as queue_app, register_root_shortcuts
from litehive.cli.runner import register_root_commands as register_runner_commands
from litehive.cli.task_cli import app as task_app
from litehive.cli.workspace import register_root_commands as register_workspace_commands, status_command
from litehive.cli.worktree_cli import app as worktree_app
from litehive.lifecycle.orchestration import run_task
from litehive.tasks.queue import dequeue_next_task

app = make_typer()
backup_app = make_typer(invoke_without_command=True)
db_app = make_typer(invoke_without_command=True)


def _run_next_task(root: Path):
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
    return status_command(Path.cwd(), full=False)


register_workspace_commands(app)
register_runner_commands(app, backup_app, db_app)
register_root_shortcuts(app)


app.add_typer(queue_app, name="queue", help="Show the active task and queued order")
app.add_typer(task_app, name="task", help="Manage Litehive tasks")
app.add_typer(archive_app, name="archive", help="Move done tasks to the archive directory")
app.add_typer(import_app, name="import", help="Import GitHub issues or freeform specs as tasks")
app.add_typer(backup_app, name="backup", help="Create, list, and restore workspace database backups")
app.add_typer(db_app, name="db", help="Inspect and migrate the workspace database schema")
app.add_typer(worktree_app, name="worktree", help="Inspect and clean Litehive-managed task worktrees")
app.add_typer(daemon_app, name="daemon", help="Manage the Litehive pool daemon", hidden=True)
app.add_typer(pipeline_app, name="pipeline", help="Inspect the v2 pipeline state machine")
app.add_typer(agent_app, name="agent", help="Agent-restricted commands (verdict submission)")


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
