from pathlib import Path
from typing import Annotated

import typer

from litehive.agents import ENGINE_CHOICES, get_engine, intake_prompt
from litehive.cli.common import WorkspaceOption, choice, make_typer, require_subcommand
from litehive.cli.display import (
    fallback_intake_goal,
    fallback_intake_title,
    link_intake_brief_to_source,
    prepare_patch_branch as prepare_patch_branch_for_issue,
    resolve_litehive_source_root,
    workspace_project_name,
)
from litehive.cli.github_import import (
    GhAuthError,
    GhNotFoundError,
    check_gh_auth,
    detect_repo_from_remote,
    fetch_open_issues,
    find_existing_task_for_issue,
    import_single_issue,
    map_labels,
    parse_issue_ref,
)
from litehive.cli.parse import parse_acceptance_criteria
from litehive.config import load_config
from litehive.models import GitHubOrigin, UpstreamContributionOrigin, UpstreamPatchProposal
from litehive.tasks.crud import create_task as create_litehive_task, discard_created_task, require_task
from litehive.tasks.models import WorkspaceConflictError
from litehive.tasks.persistence import load_state

app = make_typer(invoke_without_command=True)


@app.callback()
def import_group(ctx: typer.Context, workspace: WorkspaceOption = Path.cwd()) -> None:
    if ctx.invoked_subcommand is not None:
        return
    require_subcommand(ctx)


@app.command("issue", help="File an upstream Litehive issue/task from the current project")
def issue(
    workspace: WorkspaceOption = Path.cwd(),
    upstream: Annotated[str, typer.Option(help="Upstream Litehive issue title or short summary")] = ...,
    type: Annotated[
        str, typer.Option(click_type=choice(["runtime_bug", "missing_feature", "config_improvement", "prompt_improvement", "engine_adapter_fix"]), help="Contribution class")
    ] = "runtime_bug",
    details: Annotated[str, typer.Option(help="Long-form details")] = "",
    acceptance_criteria: Annotated[list[str] | None, typer.Option(help="Acceptance criteria")] = None,
    source_task: Annotated[str | None, typer.Option(help="Originating task id")] = None,
    source_stage: Annotated[str | None, typer.Option(help="Originating pipeline stage")] = None,
    source_role: Annotated[str, typer.Option(help="Role filing the upstream task")] = "recovery",
    source_project: Annotated[str | None, typer.Option(help="Override source project name")] = None,
    litehive_workspace: Annotated[Path | None, typer.Option(help="Override target Litehive workspace")] = None,
    patch_branch: Annotated[str | None, typer.Option(help="Proposed fix branch name")] = None,
    patch_base: Annotated[str, typer.Option(help="Base ref for --patch-branch")] = "HEAD",
    prepare_patch_branch: Annotated[bool, typer.Option(help="Create the patch branch first")] = False,
) -> int:
    from litehive.config import ensure_workspace

    ensure_workspace(workspace)
    try:
        litehive_root = resolve_litehive_source_root(
            type("Args", (), {"workspace": workspace, "litehive_workspace": litehive_workspace})()
        )
        ensure_workspace(litehive_root)
        acceptance_criteria = parse_acceptance_criteria(acceptance_criteria)
    except ValueError as exc:
        print(f"issue failed: {exc}")
        return 1

    state = load_state(workspace)
    source_task_id = source_task or state.active_task_id
    source_task_record = None
    if source_task_id is not None:
        try:
            source_task_record = require_task(workspace, source_task_id)
        except ValueError as exc:
            print(f"issue failed: {exc}")
            return 1

    source_project = source_project or workspace_project_name(workspace)
    source_stage = source_stage or (source_task_record.pipeline_status if source_task_record is not None else None)
    source_title = source_task_record.title if source_task_record is not None else None
    patch = None
    if patch_branch:
        patch = UpstreamPatchProposal(branch=patch_branch, base_ref=patch_base, prepared=False, repo_path=str(litehive_root))
        if prepare_patch_branch:
            try:
                patch = prepare_patch_branch_for_issue(litehive_root, branch=patch_branch, base_ref=patch_base)
            except ValueError as exc:
                print(f"issue failed: {exc}")
                return 1
    elif prepare_patch_branch:
        print("issue failed: --prepare-patch-branch requires --patch-branch")
        return 1

    contribution_kind = type
    mode_value = "tasks" if contribution_kind in {"missing_feature", "config_improvement", "prompt_improvement"} else "implementation"
    task_type = "bugfix" if contribution_kind == "runtime_bug" else "adapter" if contribution_kind == "engine_adapter_fix" else None
    details = details.strip()
    goal_lines = [f"Upstream contribution from `{source_project}`.", f"Contribution kind: `{contribution_kind}`."]
    if source_task_id:
        goal_lines.append(f"Originating task: `{source_task_id}`.")
    if details:
        goal_lines.extend(["", details])
    goal = "\n".join(goal_lines)

    try:
        task = create_litehive_task(
            litehive_root,
            title=upstream,
            mode=mode_value,
            task_type=task_type,
            goal=goal,
            acceptance_criteria=None if acceptance_criteria is ... else acceptance_criteria,
            upstream_origin=UpstreamContributionOrigin(
                source_project=source_project,
                source_workspace=str(workspace.resolve()),
                source_task_id=source_task_id,
                source_task_title=source_title,
                source_stage=source_stage,
                source_role=source_role,
                contribution_kind=contribution_kind,
                summary=upstream,
                details=details,
                litehive_source_path=str(litehive_root),
                patch=patch,
            ),
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"issue failed: {exc}")
        return 1

    print(f"Created upstream task {task.id}")
    print(f"litehive_workspace: {litehive_root}")
    print(f"source_project: {source_project}")
    print(f"source_task: {source_task_id or '-'}")
    print(f"contribution_type: {contribution_kind}")
    if patch is not None:
        print(f"patch_branch: {patch.branch}")
        print(f"patch_base: {patch.base_ref or '-'}")
        print(f"patch_prepared: {'yes' if patch.prepared else 'no'}")
    return 0


@app.command("spec", help="Create a rough task from a freeform spec using an LLM")
def spec(
    file: Annotated[Path | None, typer.Argument(help="File containing the brain dump")] = None,
    workspace: WorkspaceOption = Path.cwd(),
    engine: Annotated[str, typer.Option(click_type=choice(ENGINE_CHOICES), help="Engine to use")] = "opencode",
    model: Annotated[str | None, typer.Option(help="Model override")] = None,
) -> int:
    from litehive.config import ensure_workspace
    from litehive.tasks.paths import task_dir
    from litehive.tasks.templates import task_brief_file

    ensure_workspace(workspace)
    brain_dump = ""
    if file:
        try:
            brain_dump = file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Failed to read file: {exc}")
            return 1
    else:
        import sys

        try:
            if sys.stdin.isatty():
                print("Reading brain dump from stdin (Ctrl-D to end):")
            brain_dump = sys.stdin.read()
        except EOFError:
            pass

    if not brain_dump.strip():
        print("Empty brain dump; aborting.")
        return 1

    config = load_config(workspace)
    engine_name = engine or config.default_engine
    engine_impl = get_engine(engine_name)
    model = model or (config.opencode_model if engine_name == "opencode" else None)
    prompt = intake_prompt(brain_dump)
    print(f"Analyzing brain dump with {engine_name}...")

    raw_title = fallback_intake_title(brain_dump)
    raw_goal = ""
    try:
        execution = engine_impl.run(prompt, cwd=workspace, model=model)
        if execution.exit_code == 0:
            transcript = engine_impl.render_transcript(execution)
            import re as _re

            title_match = _re.search(r"^TITLE:\s*(.+)$", transcript, _re.MULTILINE)
            goal_match = _re.search(r"^GOAL:\s*(.+)$", transcript, _re.MULTILINE)
            if title_match:
                raw_title = title_match.group(1).strip()
            if goal_match:
                raw_goal = goal_match.group(1).strip()
        else:
            print(f"Warning: Analysis failed with exit code {execution.exit_code}. Creating task from raw intake.")
    except Exception as exc:
        print(f"Warning: Analysis failed ({exc}). Creating task from raw intake.")

    task = None
    try:
        task_goal = raw_goal.strip() if raw_goal.strip() else fallback_intake_goal(brain_dump)
        task_goal += "\n\n(See intake.md for the original brain dump)"
        task = create_litehive_task(
            workspace,
            title=raw_title,
            goal=task_goal,
            mode="tasks",
            task_type="intake",
            model=model,
        )
        base = task_dir(workspace, task)
        (base / "intake.md").write_text(brain_dump, encoding="utf-8")
        brief_file = task_brief_file(workspace, task)
        link_intake_brief_to_source(brief_file)
    except Exception as exc:
        if task is not None:
            discard_created_task(workspace, task.id)
        print(f"Task creation failed: {exc}")
        return 1

    print(f"Created task {task.id}: {task.title}")
    if task.goal:
        print(f"Goal: {task.goal}")
    print(f"Original dump preserved at: {base / 'intake.md'}")
    return 0


@app.command("github", help="Import GitHub issues as Litehive tasks")
def github(
    workspace: WorkspaceOption = Path.cwd(),
    issue_ref: Annotated[str | None, typer.Argument(help="GitHub issue URL or issue number")] = None,
    repo: Annotated[str | None, typer.Option(help="GitHub repo as owner/repo")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Import all open issues")] = False,
) -> int:
    from litehive.config import ensure_workspace

    ensure_workspace(workspace)
    if all_:
        try:
            check_gh_auth(cwd=workspace)
        except (GhNotFoundError, GhAuthError) as exc:
            print(f"import-issues failed: {exc}")
            return 1
        if repo is None:
            try:
                repo = detect_repo_from_remote(cwd=workspace)
            except RuntimeError as exc:
                print(f"import-issues failed: {exc}")
                return 1
        try:
            issues = fetch_open_issues(repo, cwd=workspace)
        except RuntimeError as exc:
            print(f"import-issues failed: {exc}")
            return 1
        created = 0
        skipped = 0
        errors = 0
        for issue_data in issues:
            issue_number = issue_data["number"]
            existing = find_existing_task_for_issue(workspace, repo, issue_number)
            if existing is not None:
                skipped += 1
                continue
            labels = [lbl["name"] for lbl in issue_data.get("labels", [])]
            task_type, priority = map_labels(labels)
            try:
                task = create_litehive_task(
                    workspace,
                    title=issue_data["title"],
                    goal=issue_data.get("body") or "",
                    task_type=task_type,
                    priority=priority,
                    github_origin=GitHubOrigin(repo=repo, issue_number=issue_number, issue_url=issue_data["url"]),
                    auto_commit=True,
                )
                print(f"Created task {task.id} from {repo}#{issue_number}: {task.title}")
                created += 1
            except (ValueError, WorkspaceConflictError) as exc:
                print(f"  error importing #{issue_number}: {exc}")
                errors += 1
        print(f"\nImported {created} issue(s), skipped {skipped} already-tracked issue(s).")
        return 0 if errors == 0 else 1

    if not issue_ref:
        print("import github failed: provide an issue reference or use --all")
        return 1
    try:
        check_gh_auth(cwd=workspace)
    except (GhNotFoundError, GhAuthError) as exc:
        print(f"import-issue failed: {exc}")
        return 1

    repo_from_ref, issue_number = parse_issue_ref(issue_ref)
    repo = repo or repo_from_ref
    if repo is None:
        try:
            repo = detect_repo_from_remote(cwd=workspace)
        except RuntimeError as exc:
            print(f"import-issue failed: {exc}")
            return 1

    try:
        task_id, status = import_single_issue(workspace, repo, issue_number, cwd=workspace)
    except (RuntimeError, ValueError, WorkspaceConflictError) as exc:
        print(f"import-issue failed: {exc}")
        return 1
    if status == "skipped":
        print(f"Issue #{issue_number} already imported as {task_id}, skipping.")
        return 0
    print(f"Created task {task_id} from {repo}#{issue_number}")
    return 0
