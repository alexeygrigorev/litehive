import argparse
from pathlib import Path

from litehive.config import available_process_profiles, VALID_POOL_SELECTION_POLICIES
from litehive.engines import ENGINE_CHOICES
from litehive.tasks import (
    VALID_HUMAN_CHECKPOINTS,
    VALID_PM_COMPLEXITIES,
    VALID_PLANNED_EFFORTS,
    VALID_TASK_PRIORITIES,
)

from litehive.cli._parse import TASK_TYPE_CHOICES

UPSTREAM_CONTRIBUTION_CHOICES = [
    "runtime_bug",
    "missing_feature",
    "config_improvement",
    "prompt_improvement",
    "engine_adapter_fix",
]


def build_parser():
    parser = argparse.ArgumentParser(prog="litehive")
    subparsers = parser.add_subparsers(dest="command")

    configure = subparsers.add_parser("configure", help="Initialize litehive workspace config")
    configure.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root where .litehive/ should be created",
    )
    configure.add_argument(
        "--default-engine",
        default="codex",
        help="Default engine adapter name",
    )
    configure.add_argument(
        "--process-profile",
        choices=available_process_profiles(),
        default="generic",
        help="Prompt/process overlay preset for workspace initialization",
    )
    configure.add_argument(
        "--default-retry-limit",
        type=int,
        default=3,
        help="Default retry limit for tasks without a task-specific override",
    )
    configure.add_argument(
        "--litehive-source-path",
        default=None,
        help="Path to the Litehive source repo/workspace used for upstream issue filing and patch handoff",
    )
    configure.add_argument(
        "--opencode-model",
        default="zai-coding-plan/glm-5.1",
        help="Default model identifier when using the opencode adapter",
    )
    configure.add_argument(
        "--gemini-model",
        default=None,
        help="Default model identifier when using the gemini adapter",
    )
    configure.add_argument(
        "--copilot-model",
        default=None,
        help="Default model identifier when using the copilot adapter",
    )
    configure.add_argument(
        "--claude-enabled",
        action="store_true",
        help="Enable the claude adapter (opt-in; disabled by default to protect quota)",
    )
    configure.add_argument(
        "--claude-model",
        default="claude-sonnet-4-20250514",
        help="Default model identifier when using the claude adapter",
    )
    configure.add_argument(
        "--claude-max-turns",
        type=int,
        default=30,
        help="Maximum conversation turns per claude invocation (guardrail against accidental quota burn)",
    )
    configure.add_argument(
        "--pool-usage-cap",
        type=int,
        default=None,
        help="Default pool behavior: stop before starting another engine invocation once this many invocations have run",
    )
    configure.add_argument(
        "--pool-cost-cap",
        type=int,
        default=None,
        help="Default pool behavior: stop before starting another engine invocation once this many cost units have been spent",
    )
    configure.add_argument(
        "--engine-usage-cap",
        action="append",
        default=None,
        help="Per-engine invocation cap as ENGINE=COUNT; repeat to set multiple engines",
    )
    configure.add_argument(
        "--engine-budget-cap",
        action="append",
        default=None,
        help="Per-engine budget cap in cost units as ENGINE=UNITS; repeat to set multiple engines",
    )
    configure.add_argument(
        "--engine-cost",
        action="append",
        default=None,
        help="Per-engine cost per invocation as ENGINE=UNITS; repeat to override defaults",
    )
    configure.add_argument(
        "--pool-stop-on-failure",
        action="store_true",
        help="Default pool behavior: stop after the first task that does not finish successfully",
    )
    configure.add_argument(
        "--pool-max-tasks",
        type=int,
        default=None,
        help="Default pool behavior: stop after completing this many tasks",
    )
    configure.add_argument(
        "--pool-stop-on-limit",
        action="store_true",
        help="Default pool behavior: stop after a quota, budget, rate, credit, or similar execution limit is hit",
    )
    configure.add_argument(
        "--pool-quota-threshold",
        type=int,
        default=None,
        help="Default pool behavior: stop after this many quota-like limit outcomes in a run",
    )
    configure.add_argument(
        "--pool-budget-threshold",
        type=int,
        default=None,
        help="Default pool behavior: stop after this many budget-like limit outcomes in a run",
    )
    configure.add_argument(
        "--pool-stop-on-dirty-git",
        action="store_true",
        help="Default pool behavior: stop when the git worktree is dirty before starting another task",
    )
    configure.add_argument(
        "--pool-selection-policy",
        choices=sorted(VALID_POOL_SELECTION_POLICIES),
        default="dependency_aware",
        help="Default pool task ordering policy",
    )
    configure.add_argument(
        "--hook",
        action="append",
        default=None,
        help=(
            "Add a runner hook as HOOK_POINT=blocking|nonblocking:COMMAND. "
            "Supported points: before_swe_implementation, after_swe_implementation, "
            "before_pm_acceptance, after_pm_acceptance, after_merge."
        ),
    )
    configure.add_argument(
        "--subagent-resource-limits",
        dest="subagent_resource_limits_enabled",
        action="store_true",
        default=None,
        help="Enable container-level memory/CPU/process caps for subagent execution",
    )
    configure.add_argument(
        "--no-subagent-resource-limits",
        dest="subagent_resource_limits_enabled",
        action="store_false",
        help="Disable container-level memory/CPU/process caps for subagent execution",
    )
    configure.add_argument(
        "--subagent-memory-mb",
        type=int,
        default=None,
        help="Container memory cap in MiB for subagent execution",
    )
    configure.add_argument(
        "--subagent-cpu-count",
        type=float,
        default=None,
        help="Container CPU cap for subagent execution",
    )
    configure.add_argument(
        "--subagent-process-limit",
        type=int,
        default=None,
        help="Container process-count cap for subagent execution",
    )

    status = subparsers.add_parser("status", help="Show workspace status")
    status.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )
    status.add_argument(
        "--full",
        action="store_true",
        help="Include the full per-task status dump.",
    )
    status.add_argument(
        "--fast",
        action="store_true",
        help="Read workspace state first and skip runtime hydration for faster summaries",
    )

    engine = subparsers.add_parser(
        "engine",
        help="Manage the workspace default engine",
    )
    engine.add_argument(
        "engine_action",
        choices=[*ENGINE_CHOICES, "set", "freeze", "unfreeze", "status"],
        help="Engine name (shorthand for 'set') or subcommand: set, freeze, unfreeze, status",
    )
    engine.add_argument(
        "engine_name",
        nargs="?",
        default=None,
        help="Engine name (required for set/freeze/unfreeze subcommands)",
    )
    engine.add_argument(
        "--until",
        default=None,
        help="Freeze until this date/datetime (local timezone, e.g. 2026-04-08 or '2026-04-08 09:47')",
    )
    engine.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    queue = subparsers.add_parser("queue", help="Show the active task and queued order")
    queue.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    repair = subparsers.add_parser(
        "repair",
        help="Repair stale active tasks, interrupted runs, and queue inconsistencies",
    )
    repair.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    tasks = subparsers.add_parser("tasks", help="Open the task view")
    tasks.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    web = subparsers.add_parser("web", help="Serve the local queue and session monitor")
    web.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )
    web.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1 for local-only access)",
    )
    web.add_argument(
        "--port",
        type=int,
        default=8765,
        help="TCP port to bind",
    )

    daemon = subparsers.add_parser("daemon", help="Manage the Litehive pool daemon")
    daemon_subparsers = daemon.add_subparsers(dest="daemon_command")

    daemon_run = daemon_subparsers.add_parser("run", help="Start the workspace daemon")
    daemon_run.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )
    daemon_run.add_argument(
        "--foreground",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    daemon_status = daemon_subparsers.add_parser("status", help="Show daemon state for a workspace")
    daemon_status.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    daemon_stop = daemon_subparsers.add_parser("stop", help="Stop the workspace daemon")
    daemon_stop.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    daemon_restart = daemon_subparsers.add_parser("restart", help="Restart the workspace daemon")
    daemon_restart.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    daemon_instances = daemon_subparsers.add_parser(
        "instances", help="List all live Litehive daemons"
    )

    daemon_worker = daemon_subparsers.add_parser("worker", help=argparse.SUPPRESS)
    daemon_worker.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help=argparse.SUPPRESS,
    )

    add = subparsers.add_parser("add", help="Create a queued task")
    add.add_argument("title", help="Task title")
    add.add_argument("--goal", default="", help="Task goal text")
    add.add_argument(
        "--pm-complexity",
        choices=sorted(VALID_PM_COMPLEXITIES),
        help="Initial PM complexity estimate",
    )
    add.add_argument(
        "--planned-effort",
        choices=sorted(VALID_PLANNED_EFFORTS),
        help="Initial PM planned effort size",
    )
    add.add_argument(
        "--acceptance-criteria",
        action="append",
        default=None,
        help="Add one acceptance criterion; repeat the flag for structured criteria",
    )
    add.add_argument(
        "--depends-on",
        action="append",
        help="Add prerequisite task ids; repeat the flag or use a comma-separated list",
    )
    add.add_argument(
        "--human-checkpoint",
        action="append",
        default=None,
        choices=sorted(VALID_HUMAN_CHECKPOINTS),
        help="Pause the task before this stage boundary; repeat for multiple checkpoints",
    )
    add.add_argument(
        "--task-type",
        choices=TASK_TYPE_CHOICES,
        help="Explicit routing class for this task",
    )
    add.add_argument(
        "--single",
        action="store_true",
        default=False,
        help="Use single-agent pipeline mode: one agent completes the task with no grooming, QA, or review stages",
    )
    add.add_argument(
        "--mode",
        choices=["implementation", "tasks"],
        help="Task creation mode; defaults to `tasks` when `--task-type` is set, otherwise `implementation`",
    )
    add.add_argument(
        "--priority",
        choices=sorted(VALID_TASK_PRIORITIES),
        help="Task priority; defaults to medium when omitted",
    )
    add.add_argument("--engine", choices=ENGINE_CHOICES, help="Preferred engine for the task")
    add.add_argument(
        "--model",
        help="Preferred model override for supported engines on this task",
    )
    add.add_argument(
        "--retry-limit",
        type=int,
        help="Per-task retry limit override; omit to use the workspace default",
    )
    add.add_argument(
        "--no-auto-commit",
        action="store_true",
        help="Disable auto-commit for this task",
    )
    add.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    issue = subparsers.add_parser(
        "issue",
        help="File an upstream Litehive issue/task from the current project",
    )
    issue.add_argument(
        "--upstream",
        required=True,
        help="Upstream Litehive issue title or short summary",
    )
    issue.add_argument(
        "--type",
        choices=UPSTREAM_CONTRIBUTION_CHOICES,
        default="runtime_bug",
        help="Contribution class for the upstream task",
    )
    issue.add_argument(
        "--details",
        default="",
        help="Long-form details, reproduction notes, or requested change",
    )
    issue.add_argument(
        "--acceptance-criteria",
        action="append",
        default=None,
        help="Add acceptance criteria for the upstream task; repeat for multiple",
    )
    issue.add_argument(
        "--source-task",
        default=None,
        help="Originating task id in the current project; defaults to the active task if any",
    )
    issue.add_argument(
        "--source-stage",
        default=None,
        help="Originating pipeline stage in the current project",
    )
    issue.add_argument(
        "--source-role",
        default="recovery",
        help="Role filing the upstream task",
    )
    issue.add_argument(
        "--source-project",
        default=None,
        help="Override the source project name shown in the upstream task",
    )
    issue.add_argument(
        "--litehive-workspace",
        type=Path,
        default=None,
        help="Override the target Litehive repo/workspace instead of using litehive_source_path",
    )
    issue.add_argument(
        "--patch-branch",
        default=None,
        help="Branch name in the Litehive repo for a proposed fix handoff",
    )
    issue.add_argument(
        "--patch-base",
        default="HEAD",
        help="Base ref used when preparing --patch-branch (default: HEAD)",
    )
    issue.add_argument(
        "--prepare-patch-branch",
        action="store_true",
        help="Create the patch branch in the Litehive repo before filing the task",
    )
    issue.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    intake = subparsers.add_parser(
        "intake",
        help="Create a rough task from a freeform brain dump using an LLM",
    )
    intake.add_argument(
        "file",
        type=Path,
        nargs="?",
        help="File containing the brain dump; omit to read from stdin",
    )
    intake.add_argument(
        "--engine",
        choices=ENGINE_CHOICES,
        default="opencode",
        help="Engine to use for analysis",
    )
    intake.add_argument(
        "--model",
        help="Model override for the selected engine",
    )
    intake.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    run = subparsers.add_parser("run", help="Run the next task once")

    run.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the planned selection for single-task or drain mode without invoking any agents",
    )
    run.add_argument(
        "--drain",
        action="store_true",
        help="Drain the task pool until it reaches an explicit stop condition",
    )
    run.add_argument(
        "--parallel",
        action="store_true",
        help="Run multiple independent tasks in parallel using separate worktrees (task-level parallelism)",
    )
    run.add_argument(
        "--engine",
        choices=ENGINE_CHOICES,
        help="Override the engine for this run only",
    )
    run.add_argument(
        "--model",
        help="Override the model for supported engines for this run only",
    )
    run.add_argument(
        "--stop-on-failure",
        action="store_true",
        default=None,
        help="Stop the pool after the first task that does not finish successfully",
    )
    run.add_argument(
        "--max-tasks",
        type=int,
        help="Stop the pool after completing this many tasks",
    )
    run.add_argument(
        "--stop-on-limit",
        action="store_true",
        default=None,
        help="Stop the pool after a quota, budget, rate, credit, or similar execution limit is hit",
    )
    run.add_argument(
        "--quota-threshold",
        type=int,
        help="Stop the pool after this many quota-like limit outcomes in the current run",
    )
    run.add_argument(
        "--budget-threshold",
        type=int,
        help="Stop the pool after this many budget-like limit outcomes in the current run",
    )
    run.add_argument(
        "--pool-usage-cap",
        type=int,
        help="Stop before starting another engine invocation once this many invocations have run in the current pool",
    )
    run.add_argument(
        "--pool-cost-cap",
        type=int,
        help="Stop before starting another engine invocation once this many cost units have been spent in the current pool",
    )
    run.add_argument(
        "--engine-usage-cap",
        action="append",
        default=None,
        help="Per-engine invocation cap for this run as ENGINE=COUNT; repeat to set multiple engines",
    )
    run.add_argument(
        "--engine-budget-cap",
        action="append",
        default=None,
        help="Per-engine budget cap for this run in cost units as ENGINE=UNITS; repeat to set multiple engines",
    )
    run.add_argument(
        "--engine-cost",
        action="append",
        default=None,
        help="Per-engine cost for this run as ENGINE=UNITS; repeat to override defaults",
    )
    run.add_argument(
        "--stop-on-dirty-git",
        action="store_true",
        default=None,
        help="Stop the pool when the git worktree is dirty before starting another task",
    )
    run.add_argument(
        "--parallel",
        action="store_true",
        help="Run multiple independent tasks in parallel using separate worktrees (task-level parallelism)",
    )

    dirty_worktree_gate = subparsers.add_parser(
        "dirty-worktree-gate",
        help="Report whether dirty git state should block the workspace and explain ownership",
    )
    dirty_worktree_gate.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    rollback = subparsers.add_parser(
        "rollback", help="Revert a task checkpoint commit and requeue the task"
    )
    rollback.add_argument("task_id", help="Task id to roll back")
    rollback.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    recover = subparsers.add_parser(
        "recover", help="Requeue a completed task without reverting code"
    )
    recover.add_argument("task_id", help="Task id to recover")
    recover.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    move = subparsers.add_parser("move", help="Move a queued task to a 1-based position")
    move.add_argument("task_id", help="Queued task id to move")
    move.add_argument("position", type=int, help="Target queue position (1-based)")
    move.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    prioritize = subparsers.add_parser(
        "prioritize",
        help="Move multiple queued tasks to the front in the requested order",
    )
    prioritize.add_argument(
        "task_ids",
        nargs="+",
        help="Queued task ids to move to the front, in the requested order",
    )
    prioritize.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    promote = subparsers.add_parser("promote", help="Move a queued task to the front of the queue")
    promote.add_argument("task_id", help="Queued task id to promote")
    promote.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    requeue = subparsers.add_parser("requeue", help="Requeue a flagged or closed task")
    requeue.add_argument("task_id", help="Task id to requeue")
    requeue.add_argument(
        "--front",
        action="store_true",
        help="Insert the task at the front of the queue",
    )
    requeue.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    resume = subparsers.add_parser(
        "resume",
        help="Resume an interrupted, parked, flagged, or closed task from its current stage",
    )
    resume.add_argument("task_id", help="Task id to resume")
    resume.add_argument(
        "--front",
        action="store_true",
        help="Insert the task at the front of the queue",
    )
    resume.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    abandon = subparsers.add_parser(
        "abandon", help="Cancel a flagged or closed task and remove it from the queue"
    )
    abandon.add_argument("task_id", help="Task id to abandon")
    abandon.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    stop = subparsers.add_parser("stop", help="Stop the current active task cleanly")
    stop.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    switch = subparsers.add_parser(
        "switch",
        help="Stop or resume a task, switch its task-level engine override, and queue it for the next iteration",
    )
    switch.add_argument("task_id", help="Task id to switch")
    switch.add_argument(
        "engine", choices=sorted(ENGINE_CHOICES), help="Task-level engine override to persist"
    )
    switch.add_argument(
        "--reason",
        required=True,
        help="Why the engine switch happened; recorded in the task thread comment",
    )
    switch.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    close = subparsers.add_parser(
        "close",
        help="Close a task with an explicit non-implementation outcome (wont_do, deferred, duplicate)",
    )
    close.add_argument("task_id", help="Task id to close")
    close.add_argument(
        "--outcome",
        required=True,
        choices=["wont_do", "deferred", "duplicate"],
        help="Reason the task is being closed without implementation",
    )
    close.add_argument(
        "--reason",
        default=None,
        help="Optional free-text rationale recorded in the task journal",
    )
    close.add_argument(
        "--follow-up-task",
        default=None,
        help="Optional existing task id linked as the follow-up for this close decision",
    )
    close.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    update = subparsers.add_parser("update", help="Update task engine and metadata")
    update.add_argument("task_id", help="Task id to update")
    update.add_argument(
        "--engine",
        choices=[*ENGINE_CHOICES, "default"],
        help="Override task engine, or use 'default' to clear the override",
    )
    update.add_argument(
        "--model",
        help="Override task model, or use 'default' to clear the override",
    )
    update.add_argument(
        "--retry-limit",
        type=str,
        help="Set task retry limit, or use 'default' to clear the override",
    )
    update.add_argument(
        "--priority",
        choices=sorted(VALID_TASK_PRIORITIES),
        help="Set task priority",
    )
    update.add_argument(
        "--pm-complexity",
        choices=[*sorted(VALID_PM_COMPLEXITIES), "none"],
        help="Set PM complexity, or use 'none' to clear it",
    )
    update.add_argument(
        "--planned-effort",
        choices=[*sorted(VALID_PLANNED_EFFORTS), "none"],
        help="Set planned effort, or use 'none' to clear it",
    )
    update.add_argument("--goal", help="Replace the task goal text")
    update.add_argument(
        "--depends-on",
        action="append",
        help="Replace task dependencies; repeat the flag or use a comma-separated list, or use 'none' to clear",
    )
    update.add_argument(
        "--acceptance-criteria",
        action="append",
        default=None,
        help="Replace acceptance criteria; repeat the flag or use 'none' to clear",
    )
    update.add_argument(
        "--constraint",
        action="append",
        default=None,
        help="Replace constraints; repeat the flag or use 'none' to clear",
    )
    update.add_argument(
        "--plan-step",
        action="append",
        default=None,
        help="Replace the task plan; repeat the flag or use 'none' to clear",
    )
    update.add_argument(
        "--human-checkpoint",
        action="append",
        default=None,
        choices=[*sorted(VALID_HUMAN_CHECKPOINTS), "none"],
        help="Replace human checkpoints; repeat the flag or use 'none' to clear",
    )
    update.add_argument(
        "--task-type",
        choices=[*TASK_TYPE_CHOICES, "default"],
        help="Override task routing class, or use 'default' to clear it",
    )
    update.add_argument(
        "--mode",
        choices=["tasks", "implementation"],
        help="Set task mode",
    )
    update.add_argument(
        "--auto-commit",
        dest="auto_commit",
        action="store_true",
        default=None,
        help="Enable auto-commit for this task",
    )
    update.add_argument(
        "--no-auto-commit",
        dest="auto_commit",
        action="store_false",
        help="Disable auto-commit for this task",
    )
    update.add_argument(
        "--from-file",
        type=Path,
        help="Load rich task updates from a YAML file mapped onto durable task fields",
    )
    update.add_argument(
        "--edit",
        action="store_true",
        help="Open the current task shaping fields in $VISUAL or $EDITOR and persist the edited YAML",
    )
    update.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    report = subparsers.add_parser("report", help="Submit a stage verdict for the active task")
    report.add_argument(
        "--verdict", required=True, choices=["pass", "fail", "reject", "blocked", "comment"]
    )
    report.add_argument(
        "--message",
        required=True,
        help="Detailed explanation of what was done, what failed, or what needs fixing",
    )
    report.add_argument(
        "--role", default="swe", help="Role submitting the report (swe, qa, reviewer, planner)"
    )
    report.add_argument(
        "--step",
        help="Stage (grooming, implementing, testing, accepting). Defaults to the task's current pipeline_status.",
    )
    report.add_argument("--task-id", help="Task ID. Defaults to the active task.")
    report.add_argument(
        "--workspace", type=Path, default=Path.cwd(), help="Repository root containing .litehive/"
    )

    # ── import-issue / import-issues ───────────────────────────────────
    import_issue = subparsers.add_parser(
        "import-issue",
        help="Import a single GitHub issue as a litehive task",
    )
    import_issue.add_argument(
        "issue_ref",
        help="GitHub issue URL (https://github.com/owner/repo/issues/N) or issue number",
    )
    import_issue.add_argument(
        "--repo",
        default=None,
        help="GitHub repo as owner/repo; auto-detected from git remote if omitted",
    )
    import_issue.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    import_issues = subparsers.add_parser(
        "import-issues",
        help="Import all open GitHub issues that don't already have litehive tasks",
    )
    import_issues.add_argument(
        "--repo",
        default=None,
        help="GitHub repo as owner/repo; auto-detected from git remote if omitted",
    )
    import_issues.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    # ── list ──────────────────────────────────────────────────────────
    list_cmd = subparsers.add_parser(
        "list", help="Compact task listing with optional filters"
    )
    list_cmd.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Include done tasks (excluded by default)",
    )
    list_cmd.add_argument(
        "--status",
        dest="filter_status",
        help="Filter by task status (queued, in_progress, done, …)",
    )
    list_cmd.add_argument(
        "--pipeline-status",
        dest="filter_pipeline_status",
        help="Filter by pipeline stage (backlog, implementing, …)",
    )
    list_cmd.add_argument(
        "--engine",
        dest="filter_engine",
        help="Filter by engine name",
    )
    list_cmd.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    # ── show ──────────────────────────────────────────────────────────
    show_cmd = subparsers.add_parser(
        "show", help="Print full details for a single task"
    )
    show_cmd.add_argument("task_id", help="Task ID (e.g. T-0001)")
    show_cmd.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    # ── archive ───────────────────────────────────────────────────────
    archive_cmd = subparsers.add_parser(
        "archive", help="Move done tasks to the archive directory"
    )
    archive_cmd.set_defaults(command_parser=archive_cmd)
    archive_cmd.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="Task ID to archive",
    )
    archive_cmd.add_argument(
        "--all-done",
        action="store_true",
        help="Archive all done tasks and skip missing or broken task references",
    )
    archive_cmd.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    # ── cleanup ──────────────────────────────────────────────────────
    cleanup_cmd = subparsers.add_parser(
        "cleanup", help="Delete archived tasks older than a given duration"
    )
    cleanup_cmd.add_argument(
        "--older-than",
        required=True,
        dest="older_than",
        help="Duration threshold (e.g. 30d, 24h, 60m)",
    )
    cleanup_cmd.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    return parser
