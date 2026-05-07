"""
Standalone pipeline CLI commands.

Imported directly by ``cli.app`` and exposed under ``litehive
pipeline``. Kept independent of the root app's other groups so the
pipeline diagnostics surface (rules, set-state, reset, journal) can
be wired into smaller test harnesses without dragging in queue,
worktree, and daemon command modules.
"""

from pathlib import Path
from typing import Annotated

import typer

from litehive.cli.common import make_typer
from litehive.container import build_pipeline_container
from litehive.domain.common import canonical_pipeline_state
from litehive.lifecycle.persistence import TaskNotFound, TaskState
from litehive.lifecycle.transitions import list_transitions
from litehive.tasks.report_storage import latest_recovery_report, latest_stage_report
from litehive.workspace import Workspace

app = make_typer(invoke_without_command=True)


@app.command("rules", help="List pipeline transition rules as readable rows")
def pipeline_rules_command() -> int:
    """
    Dump the in-process pipeline transition table.

    Renders each registered ``(from_state, event) -> to_state`` rule
    as one line so operators can audit which transitions the state
    machine accepts without reading ``lifecycle/transitions.py``.
    Frozen-set sources are joined with ``|`` and callable
    destinations are bracketed by their function name.
    """
    for rule in list_transitions():
        if isinstance(rule.from_state, frozenset):
            from_state = "|".join(str(stage) for stage in sorted(rule.from_state))
        else:
            from_state = str(rule.from_state)
        if not callable(rule.transition_to):
            to = rule.transition_to
        else:
            to = f"<{rule.transition_to.__name__}>"
        event_name = rule.on_event.__name__
        if rule.description:
            desc = f"  # {rule.description}"
        else:
            desc = ""
        print(f"{from_state:25s} --[{event_name:25s}]--> {to}{desc}")
    return 0


@app.command("set-state", help="Override a task's pipeline stage")
def pipeline_set_state_command(
    task_id: Annotated[str, typer.Argument(help="Task id")],
    stage: Annotated[str, typer.Argument(help="Target stage")],
    workspace: Annotated[Path, typer.Option("--workspace", help="Workspace root")] = Path.cwd(),
) -> None:
    """
    Force a task's pipeline stage to an arbitrary value.

    Operator escape hatch for when the state machine has parked a
    task in a stage that automated transitions cannot exit (e.g.
    after a corrupted recovery trigger or a missing report). The
    target value is normalized through
    :func:`canonical_pipeline_state` so persisted history stays
    enum-typed instead of carrying free-form strings.
    """
    pipeline = build_pipeline_container(workspace)
    store = pipeline.persistence
    try:
        state = store.load(task_id)
    except TaskNotFound:
        print(f"no pipeline state row for {task_id}; create task pipeline state first")
        raise typer.Exit(1)
    old_stage = state.stage
    state.stage = canonical_pipeline_state(stage)
    store.save(state)
    print(f"task: {task_id}")
    print(f"stage: {old_stage} → {stage}")


@app.command("reset", help="Clear all pipeline state for a task so it starts fresh")
def pipeline_reset_command(
    task_id: Annotated[str, typer.Argument(help="Task id")],
    workspace: Annotated[Path, typer.Option("--workspace", help="Workspace root")] = Path.cwd(),
) -> None:
    """
    Wipe all persisted pipeline state for one task.

    Used when accumulated retry counters, recovery triggers, or
    rejection history would otherwise short-circuit the runner the
    next time it picks up the task. Goes through ``reset_all`` so
    every column managed by :class:`SqlitePersistence` is cleared
    in a single transaction — no half-reset state on partial
    failure.
    """
    pipeline = build_pipeline_container(workspace)
    store = pipeline.persistence
    store.reset_all(task_id)
    print(f"task: {task_id}")
    print("reset: ok")


@app.command("journal", help="Dump the pipeline journal and transitions for one task")
def pipeline_journal_command(
    task_id: Annotated[str, typer.Argument(help="Task id (e.g. T-0001)")],
    workspace: Annotated[Path, typer.Option("--workspace", help="Workspace root")] = Path.cwd(),
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max transitions to show")] = 50,
) -> int:
    """
    Render every persisted artifact for one task.

    Dumps current state, the latest stage and recovery reports,
    recovery history, retry counters, and the lifecycle/transition
    log so a debugger can reconstruct what happened without poking
    SQLite by hand. ``--limit`` only caps the transition log; the
    other sections are short enough to print whole.
    """
    pipeline = build_pipeline_container(workspace)
    workspace_obj = pipeline.workspace
    journal = pipeline.journal
    store = pipeline.persistence
    try:
        state = store.load(task_id)
    except TaskNotFound:
        print(f"no pipeline state row for task {task_id}")
        raise typer.Exit(1)

    print(f"task: {task_id}")
    print(f"stage: {state.stage}")
    _print_pipeline_report_lines(workspace_obj, task_id)
    _print_pipeline_state_lines(state)
    _print_pipeline_lifecycle_lines(journal.load_lifecycle(task_id))
    _print_pipeline_transition_lines(journal.load_transitions(task_id), limit)
    return 0


def _print_pipeline_report_lines(workspace: Workspace, task_id: str) -> None:
    task = workspace.get_task_record(task_id)
    if task is None:
        return
    stage_report = latest_stage_report(workspace, task)
    if stage_report is not None:
        print(
            "latest_stage_report: "
            f"{stage_report.pipeline_state}/{stage_report.verdict} "
            f"source={stage_report.source} "
            f"summary={stage_report.summary}"
        )
    recovery_report = latest_recovery_report(workspace, task)
    if recovery_report is not None:
        print(
            "latest_recovery_report: "
            f"origin_stage={recovery_report.origin_stage or '-'} "
            f"trigger_event_kind={recovery_report.trigger_event_kind.value} "
            f"runnable_state={recovery_report.runnable_state} "
            f"summary={recovery_report.summary}"
        )


def _print_pipeline_state_lines(state: TaskState) -> None:
    _print_recovery_trigger_line(state)
    _print_merge_and_commit_lines(state)
    _print_recovery_history_lines(state)
    _print_failed_run_history_lines(state)
    _print_failure_detail_lines(state)


def _print_recovery_trigger_line(state: TaskState) -> None:
    if not state.active_recovery_trigger:
        return
    trigger = state.active_recovery_trigger
    print(
        "active_recovery_trigger: "
        f"origin_stage={trigger.origin_stage} "
        f"trigger_event_kind={trigger.trigger_event_kind.value} "
        f"fingerprint={trigger.failure_fingerprint.budget_key()} "
        f"source={trigger.source or '-'} "
        f"reason_code={trigger.reason_code or '-'}"
    )


def _print_merge_and_commit_lines(state: TaskState) -> None:
    if state.merge_context is not None:
        print(
            "merge_context: "
            f"merge_attempt={state.merge_context.merge_attempt} "
            f"conflict_files={state.merge_context.conflict_files}"
        )
    if state.commit_result is not None:
        print(f"commit_result: head_sha={state.commit_result.head_sha} reason={state.commit_result.reason or '-'}")


def _print_recovery_history_lines(state: TaskState) -> None:
    if not state.recovery_history:
        return
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


def _print_failed_run_history_lines(state: TaskState) -> None:
    if not state.failed_run_history:
        return
    print("failed_run_history:")
    for key, record in state.failed_run_history.items():
        print(
            "  "
            f"{key} "
            f"stage={record.stage} "
            f"shape={record.failure_shape} "
            f"count={record.count} "
            f"latest_at={record.latest_at or '-'} "
            f"operator_override_count={record.operator_override_count}"
        )


def _print_failure_detail_lines(state: TaskState) -> None:
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


def _print_pipeline_lifecycle_lines(lifecycle) -> None:
    if not lifecycle:
        return
    print("\nlifecycle:")
    for row in lifecycle:
        print(f"  {row['seq']:3d} {row['created_at']}  {row['kind']}  {row['payload']}")


def _print_pipeline_transition_lines(transitions, limit: int) -> None:
    if not transitions:
        return
    recent = transitions[-limit:]
    print(f"\ntransitions (last {len(recent)} of {len(transitions)}):")
    for row in recent:
        desc = row["rule_description"] or ""
        if desc:
            desc_suffix = f"  # {desc}"
        else:
            desc_suffix = ""
        print(
            f"  {row['seq']:3d} {row['created_at']}  {row['from_stage']:25s} --[{row['event_type']:25s}]--> {row['to_stage']}"
            + desc_suffix
        )
