import subprocess
from pathlib import Path

from typer.testing import CliRunner

from litehive.cli.app import app as cli_app
from litehive.config.paths import workspace_path


_runner = CliRunner()


def _invoke_cli(argv: list[object], *, input_text: str | None = None) -> int:
    normalized = [str(arg) for arg in argv if arg is not None]
    result = _runner.invoke(cli_app, normalized, input=input_text, standalone_mode=False)
    if result.output:
        print(result.output, end="")
    if result.exit_code != 0:
        return result.exit_code
    if isinstance(result.return_value, int):
        return result.return_value
    return 0


def _cmd_add(args) -> int:
    argv = ["task", "add", args.title, "--workspace", args.workspace, "--goal", args.goal]
    for criterion in args.acceptance_criteria or []:
        argv.extend(["--acceptance-criteria", criterion])
    for dep in args.depends_on or []:
        argv.extend(["--depends-on", dep])
    if args.priority is not None:
        argv.extend(["--priority", args.priority])
    return _invoke_cli(argv)


def _cmd_debug(args) -> int:
    argv = ["task", "debug", args.task_id, "--workspace", args.workspace]
    if getattr(args, "all", False):
        argv.append("--all")
    if getattr(args, "worktree", False):
        argv.append("--worktree")
    return _invoke_cli(argv)


def _cmd_list(args) -> int:
    argv = ["task", "list", "--workspace", args.workspace]
    if getattr(args, "show_all", False):
        argv.append("--all")
    if getattr(args, "filter_status", None) is not None:
        argv.extend(["--status", args.filter_status])
    if getattr(args, "filter_pipeline_status", None) is not None:
        argv.extend(["--pipeline-status", args.filter_pipeline_status])
    if getattr(args, "filter_engine", None) is not None:
        argv.extend(["--engine", args.filter_engine])
    return _invoke_cli(argv)


def _cmd_logs(args) -> int:
    argv = ["task", "logs", "--workspace", args.workspace]
    if getattr(args, "task_id", None) is not None:
        argv.insert(2, args.task_id)
    if getattr(args, "daemon", False):
        argv.append("--daemon")
    if getattr(args, "agent", False):
        argv.append("--agent")
    if getattr(args, "all", False):
        argv.append("--all")
    if getattr(args, "follow", False):
        argv.append("--follow")
    return _invoke_cli(argv)


def _cmd_queue(args) -> int:
    return _invoke_cli(["queue", "--workspace", args.workspace])


def _cmd_requeue_task(args) -> int:
    argv = ["queue", "requeue", args.task_id, "--workspace", args.workspace]
    if getattr(args, "front", False):
        argv.append("--front")
    if getattr(args, "force", False):
        argv.append("--force")
    return _invoke_cli(argv)


def _cmd_show(args) -> int:
    return _invoke_cli(["task", "show", args.task_id, "--workspace", args.workspace])


def _cmd_status(args) -> int:
    argv = ["status", "--workspace", args.workspace]
    if getattr(args, "full", False):
        argv.append("--full")
    return _invoke_cli(argv)


def _cmd_update(args) -> int:
    argv = ["task", "update", args.task_id, "--workspace", args.workspace]
    for name in ["title", "priority", "goal"]:
        value = getattr(args, name, None)
        if value is not None:
            argv.extend([f"--{name.replace('_', '-')}", value])
    for option_name, values in [
        ("depends-on", getattr(args, "depends_on", None)),
        ("acceptance-criteria", getattr(args, "acceptance_criteria", None)),
    ]:
        for value in values or []:
            argv.extend([f"--{option_name}", value])
    return _invoke_cli(argv)


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _task_worktree_path(root: Path, task) -> Path:
    return workspace_path(root, "worktrees") / f"{task.id}-{task.slug}"
