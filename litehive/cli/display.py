import subprocess
from pathlib import Path

from litehive.config import load_config
from litehive.git_ops import is_git_repo
from litehive.models import UpstreamPatchProposal


def workspace_project_name(root):
    return root.resolve().name


def resolve_litehive_source_root(args):
    explicit = getattr(args, "litehive_workspace", None)
    if explicit is not None:
        return explicit.resolve()
    config = load_config(args.workspace)
    if not config.litehive_source_path:
        raise ValueError(
            "litehive_source_path is not configured; run `litehive configure --litehive-source-path <path>` "
            "or pass `--litehive-workspace`."
        )
    return Path(config.litehive_source_path).expanduser().resolve()


def _git_output(root, *args):
    proc = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def prepare_patch_branch(root, *, branch, base_ref):
    if not is_git_repo(root):
        raise ValueError(f"{root} is not a git repository")
    branch = branch.strip()
    if not branch:
        raise ValueError("Patch branch name cannot be empty")
    existing = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode != 0:
        _git_output(root, "rev-parse", "--verify", base_ref)
        proc = subprocess.run(
            ["git", "branch", branch, base_ref],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError(proc.stderr.strip() or "git branch failed")
    return UpstreamPatchProposal(
        branch=branch,
        base_ref=base_ref,
        prepared=True,
        repo_path=str(root),
    )


def fallback_intake_title(brain_dump):
    first_nonempty = next((line.strip() for line in brain_dump.splitlines() if line.strip()), "")
    if not first_nonempty:
        return "Unstructured Intake"
    compact = " ".join(first_nonempty.split())
    return compact[:77] + "..." if len(compact) > 80 else compact


def fallback_intake_goal(brain_dump):
    lines = [" ".join(line.split()) for line in brain_dump.splitlines() if line.strip()]
    if not lines:
        return "Capture the original intake and prepare it for planner grooming."
    summary = " ".join(lines)
    summary = summary[:237] + "..." if len(summary) > 240 else summary
    return summary


def link_intake_brief_to_source(brief_file):
    if not brief_file.exists():
        return
    content = brief_file.read_text(encoding="utf-8")
    stub_pattern = "### Intake Notes\n- Capture the core brain dump or link to the source.\n\n_TBD_"
    replacement = (
        "### Intake Notes\n"
        "- Original dump: [intake.md](intake.md)\n"
        "- Treat `intake.md` as the authoritative source for the raw specification.\n"
    )
    if stub_pattern in content:
        brief_file.write_text(content.replace(stub_pattern, replacement), encoding="utf-8")


def format_engine_int_map(values):
    if not values:
        return "-"
    return ", ".join(f"{engine}={limit}" for engine, limit in sorted(values.items()))


def format_retry_on(config):
    if not config.retry_on:
        return "-"
    return ",".join(config.retry_on)


def task_engine_label(task_engine, default_engine):
    return task_engine or f"{default_engine} (default)"


def task_model_label(task_model):
    return task_model or "default"


def task_dependencies_label(task_id, dependencies):
    if not dependencies:
        return "-"
    return (
        ", ".join(dependency_id for dependency_id in dependencies if dependency_id != task_id)
        or "-"
    )


def task_interruption_label(task):
    if task.status != "interrupted" or task.runtime.current_stage.status != "interrupted":
        return ""
    interruption = task.runtime.interruption
    stage = (
        interruption.resume_stage
        if interruption is not None and interruption.resume_stage is not None
        else task.runtime.current_stage.step or task.pipeline_status
    )
    label = f" resumable_from={stage}"
    if interruption is not None:
        label += f" interruption={interruption.source}"
    if task.runtime.last_outcome.reason_code:
        label += f" reason_code={task.runtime.last_outcome.reason_code}"
    if task.runtime.last_subagent is not None:
        label += (
            " last_subagent="
            f"{task.runtime.last_subagent.id}:{task.runtime.last_subagent.role}/{task.runtime.last_subagent.engine}"
        )
    if interruption is not None and interruption.reason:
        label += f" reason={interruption.reason}"
    return label


def cli_override_or_default(value, default):
    if value is None:
        return default
    if isinstance(default, bool) and value is False:
        return default
    return value
