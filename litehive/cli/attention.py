from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import typer

from litehive.attention import list_attention, resolve_attention
from litehive.cli.common import WorkspaceOption, make_typer, require_subcommand

app = make_typer(invoke_without_command=True)


@app.callback()
def attention_group(ctx: typer.Context) -> None:
    require_subcommand(ctx)


def cmd_attention_list(args) -> int:
    items = list_attention(args.workspace)
    print(f"pending_attention: {len(items)}")
    if not items:
        print("attention: none")
        return 0
    for item in items:
        task_label = item.task_id or "-"
        print(
            f"attention: {item.id} created_at={item.created_at} kind={item.kind} "
            f"task_id={task_label} title={item.title}"
        )
        print(f"  reason: {item.reason}")
        print(f"  suggested_action: {item.suggested_action}")
    return 0


def cmd_attention_resolve(args) -> int:
    item = resolve_attention(args.workspace, args.attention_id)
    if item is None:
        print(f"attention item not found: {args.attention_id}")
        return 1
    print(f"resolved_attention: {item.id}")
    print(f"title: {item.title}")
    return 0


@app.command("list", help="List pending operator-attention items")
def attention_list_command(workspace: WorkspaceOption = Path.cwd()) -> int:
    return cmd_attention_list(SimpleNamespace(workspace=workspace))


@app.command("resolve", help="Resolve one operator-attention item by id")
def attention_resolve_command(
    attention_id: Annotated[int, typer.Argument(help="Attention item id to resolve.")],
    workspace: WorkspaceOption = Path.cwd(),
) -> int:
    return cmd_attention_resolve(SimpleNamespace(workspace=workspace, attention_id=attention_id))
