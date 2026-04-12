from pathlib import Path
from typing import Annotated

import typer

from litehive.cli.common import WorkspaceOption, make_typer
from litehive.config import ensure_workspace
from litehive.tasks.archive import archive_done_tasks, archive_task, cleanup_archived_tasks

app = make_typer(invoke_without_command=True)


@app.callback()
def archive_group(
    ctx: typer.Context,
    task_id: Annotated[str | None, typer.Argument(help="Task ID to archive")] = None,
    workspace: WorkspaceOption = Path.cwd(),
    all_done: Annotated[bool, typer.Option("--all-done", help="Archive all done tasks")] = False,
) -> int | None:
    if ctx.invoked_subcommand is not None:
        return None
    if task_id is None and not all_done:
        typer.echo(ctx.get_help())
        raise typer.Exit(2)
    ensure_workspace(workspace)
    try:
        if task_id is not None:
            task = archive_task(workspace, task_id)
            print(f"archived: {task.id} {task.title}")
            print("archived_count: 1")
        else:
            tasks = archive_done_tasks(
                workspace,
                on_skip=lambda skipped_task_id, exc: print(
                    f"archive skipped: {skipped_task_id} ({exc})"
                ),
            )
            for task in tasks:
                print(f"archived: {task.id} {task.title}")
            print(f"archived_count: {len(tasks)}")
    except ValueError as exc:
        print(f"archive failed: {exc}")
        return 1
    return 0


@app.command("cleanup", help="Delete archived tasks older than a given duration")
def cleanup(
    workspace: WorkspaceOption = Path.cwd(),
    older_than: Annotated[str, typer.Option(help="Duration threshold (e.g. 30d, 24h, 60m)")] = ...,
) -> int:
    ensure_workspace(workspace)
    try:
        deleted = cleanup_archived_tasks(workspace, older_than)
    except ValueError as exc:
        print(f"cleanup failed: {exc}")
        return 1
    for task in deleted:
        print(f"deleted: {task.id} {task.title}")
    print(f"deleted_count: {len(deleted)}")
    return 0
