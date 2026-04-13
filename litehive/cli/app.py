"""CLI entrypoint for litehive."""

from pathlib import Path
from typing import Annotated

import click
import typer

from litehive.cli.agent_cli import agent_app
from litehive.cli.archive_cli import app as archive_app
from litehive.cli.common import make_typer
from litehive.cli.daemon_cli import app as daemon_app
from litehive.cli.queue_cli import app as queue_app, register_hidden_root_commands
from litehive.cli.runner import register_root_commands as register_runner_commands
from litehive.cli.task_cli import app as task_app
from litehive.cli.workspace import register_root_commands as register_workspace_commands, status_command
from litehive.cli.worktree_cli import app as worktree_app
from litehive.pipeline.orchestration import run_task
from litehive.tasks.queue import dequeue_next_task

app = make_typer()
backup_app = make_typer(invoke_without_command=True)
db_app = make_typer(invoke_without_command=True)
pipeline_app = make_typer(invoke_without_command=True)


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
    return status_command(Path.cwd(), full=False, fast=False)

register_workspace_commands(app)
register_runner_commands(app, backup_app, db_app)
register_hidden_root_commands(app)


app.add_typer(queue_app, name="queue", help="Show the active task and queued order")
app.add_typer(task_app, name="task", help="Manage Litehive tasks")
app.add_typer(archive_app, name="archive", help="Move done tasks to the archive directory")
app.add_typer(backup_app, name="backup", help="Create, list, and restore workspace database backups")
app.add_typer(db_app, name="db", help="Inspect and migrate the workspace database schema")
app.add_typer(worktree_app, name="worktree", help="Inspect and clean Litehive-managed task worktrees")
app.add_typer(daemon_app, name="daemon", help="Manage the Litehive pool daemon", hidden=True)
app.add_typer(pipeline_app, name="pipeline", help="Inspect the v2 pipeline state machine")
app.add_typer(agent_app, name="agent", help="Agent-restricted commands (verdict submission)")


@pipeline_app.command("rules", help="List the v2 transition rules as readable rows")
def pipeline_rules_command() -> int:
    from litehive.pipeline.transitions import list_transitions

    for rule in list_transitions():
        from_state = "|".join(sorted(rule.from_state)) if isinstance(rule.from_state, frozenset) else rule.from_state
        to = rule.transition_to if not callable(rule.transition_to) else f"<{rule.transition_to.__name__}>"
        event_name = rule.on_event.__name__
        desc = f"  # {rule.description}" if rule.description else ""
        print(f"{from_state:25s} --[{event_name:25s}]--> {to}{desc}")
    return 0


@pipeline_app.command("set-state", help="Override a task's v2 pipeline stage")
def pipeline_set_state_command(
    task_id: Annotated[str, typer.Argument(help="Task id")],
    stage: Annotated[str, typer.Argument(help="Target stage")],
    workspace: Annotated[Path, typer.Option("--workspace", help="Workspace root")] = Path.cwd(),
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


@pipeline_app.command("journal", help="Dump the v2 pipeline journal + transitions for one task")
def pipeline_journal_command(
    task_id: Annotated[str, typer.Argument(help="Task id (e.g. T-0001)")],
    workspace: Annotated[Path, typer.Option("--workspace", help="Workspace root")] = Path.cwd(),
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max transitions to show")] = 50,
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
                f"  {row['seq']:3d} {row['created_at']}  {row['from_stage']:25s} --[{row['event_type']:25s}]--> {row['to_stage']}"
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
