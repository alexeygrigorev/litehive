"""Standalone pipeline CLI commands that do not depend on the full root app."""

from pathlib import Path
from typing import Annotated

import typer

from litehive.cli.common import make_typer

app = make_typer(invoke_without_command=True)


@app.command("rules", help="List the v2 transition rules as readable rows")
def pipeline_rules_command() -> int:
    from litehive.lifecycle.transitions import list_transitions

    for rule in list_transitions():
        from_state = "|".join(sorted(rule.from_state)) if isinstance(rule.from_state, frozenset) else rule.from_state
        to = rule.transition_to if not callable(rule.transition_to) else f"<{rule.transition_to.__name__}>"
        event_name = rule.on_event.__name__
        desc = f"  # {rule.description}" if rule.description else ""
        print(f"{from_state:25s} --[{event_name:25s}]--> {to}{desc}")
    return 0


@app.command("set-state", help="Override a task's v2 pipeline stage")
def pipeline_set_state_command(
    task_id: Annotated[str, typer.Argument(help="Task id")],
    stage: Annotated[str, typer.Argument(help="Target stage")],
    workspace: Annotated[Path, typer.Option("--workspace", help="Workspace root")] = Path.cwd(),
) -> None:
    from litehive.lifecycle.persistence import SqlitePersistence, TaskNotFound

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


@app.command("reset", help="Clear all v2 pipeline state for a task so it starts fresh")
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


@app.command("journal", help="Dump the v2 pipeline journal + transitions for one task")
def pipeline_journal_command(
    task_id: Annotated[str, typer.Argument(help="Task id (e.g. T-0001)")],
    workspace: Annotated[Path, typer.Option("--workspace", help="Workspace root")] = Path.cwd(),
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max transitions to show")] = 50,
) -> int:
    from litehive.lifecycle.journal import SqliteJournal
    from litehive.lifecycle.persistence import SqlitePersistence, TaskNotFound

    journal = SqliteJournal(workspace)
    store = SqlitePersistence(workspace)
    try:
        state = store.load(task_id)
    except TaskNotFound:
        print(f"no v2 state row for task {task_id}")
        raise typer.Exit(1)

    print(f"task: {task_id}")
    print(f"stage: {state.stage}")
    if state.active_recovery_trigger:
        trigger = state.active_recovery_trigger
        print(
            "active_recovery_trigger: "
            f"origin_stage={trigger.origin_stage} "
            f"trigger_event_kind={trigger.trigger_event_kind.value} "
            f"fingerprint={trigger.failure_fingerprint.budget_key()} "
            f"source={trigger.source or '-'} "
            f"reason_code={trigger.reason_code or '-'}"
        )
    if state.merge_context is not None:
        print(
            "merge_context: "
            f"merge_attempt={state.merge_context.merge_attempt} "
            f"conflict_files={state.merge_context.conflict_files}"
        )
    if state.commit_result is not None:
        print(
            "commit_result: "
            f"head_sha={state.commit_result.head_sha} "
            f"reason={state.commit_result.reason or '-'}"
        )
    if state.recovery_history:
        print("recovery_history:")
        for outcome in state.recovery_history:
            print(
                "  "
                f"{outcome.created_at} "
                f"{outcome.trigger.origin_stage or '-'} "
                f"{outcome.trigger.trigger_event_kind.value} "
                f"{outcome.recovery_verdict} "
                f"{outcome.disposition.value}"
            )
    if state.stage_retry:
        print(f"stage_retry: {dict(state.stage_retry)}")
    if state.failed_reason:
        print(f"failed_reason: {state.failed_reason}")
    if state.failed_message:
        print(f"failed_message: {state.failed_message}")
    if state.recovery_failure_explanation:
        print(f"recovery_failure_explanation: {state.recovery_failure_explanation}")
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
