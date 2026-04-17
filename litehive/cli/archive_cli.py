from pathlib import Path
from typing import Annotated

import click
import typer
from typer.core import TyperGroup

from litehive.cli.common import WorkspaceOption
from litehive.config.workspace import ensure_workspace
from litehive.tasks.archive import archive_done_tasks, archive_task, cleanup_archived_tasks


class ArchiveGroup(TyperGroup):
    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args:
            cmd_name = click.utils.make_str(args[0])
            cmd = self.get_command(ctx, cmd_name)
            if cmd is not None:
                return cmd_name, cmd, args[1:]
        return None, None, args

    def invoke(self, ctx: click.Context):
        def _process_result(value):
            if self._result_callback is not None:
                value = ctx.invoke(self._result_callback, value, **ctx.params)
            return value

        if not ctx._protected_args:
            if self.invoke_without_command:
                with ctx:
                    rv = super(TyperGroup, self).invoke(ctx)
                    return _process_result([] if self.chain else rv)
            raise click.UsageError("Missing command.", ctx)

        args = [*ctx._protected_args, *ctx.args]
        ctx.args = []
        ctx._protected_args = []

        if self.chain:
            return super().invoke(ctx)

        with ctx:
            cmd_name, cmd, args = self.resolve_command(ctx, args)
            if cmd is None:
                ctx.args = args
                ctx.invoked_subcommand = None
                if self.callback is None:
                    return _process_result(None)
                return _process_result(ctx.invoke(self.callback, **ctx.params))
            ctx.invoked_subcommand = cmd_name
            super(TyperGroup, self).invoke(ctx)
            sub_ctx = cmd.make_context(cmd_name, args, parent=ctx)
            with sub_ctx:
                return _process_result(sub_ctx.command.invoke(sub_ctx))


app = typer.Typer(
    cls=ArchiveGroup,
    add_completion=False,
    context_settings={
        "help_option_names": ["-h", "--help"],
        "allow_extra_args": True,
        "ignore_unknown_options": False,
    },
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
def archive_group(
    ctx: typer.Context,
    workspace: WorkspaceOption = Path.cwd(),
    all_done: Annotated[bool, typer.Option("--all-done", help="Archive all done tasks")] = False,
) -> int | None:
    if ctx.invoked_subcommand is not None:
        return None
    extra_args = list(ctx.args)
    if len(extra_args) > 1:
        typer.echo(ctx.get_help())
        raise typer.Exit(2)
    task_id = extra_args[0] if extra_args else None
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
                on_skip=lambda skipped_task_id, exc: print(f"archive skipped: {skipped_task_id} ({exc})"),
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
