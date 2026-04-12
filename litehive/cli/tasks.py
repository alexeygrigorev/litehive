from litehive.config import ensure_workspace, load_config
from litehive.agents import get_engine
from litehive.models import UpstreamContributionOrigin, UpstreamPatchProposal
from litehive.agents import intake_prompt
from litehive.tasks.crud import create_task, discard_created_task, require_task
from litehive.tasks.models import WorkspaceConflictError
from litehive.tasks.normalization import missing_acceptance_criteria_cli_warning
from litehive.tasks.persistence import load_state
from litehive.workspace.task_status import update_task_metadata

from litehive.cli.display import (
    fallback_intake_goal,
    fallback_intake_title,
    link_intake_brief_to_source,
    prepare_patch_branch as prepare_patch_branch_for_issue,
    resolve_litehive_source_root,
    task_dependencies_label,
    task_model_label,
    workspace_project_name,
)
from litehive.cli.parse import (
    collect_editor_task_updates,
    load_rich_task_update_file,
    merge_task_updates,
    parse_acceptance_criteria,
    parse_dependency_ids,
    parse_text_list_option,
)


def cmd_add(title, workspace, goal="", acceptance_criteria=None, depends_on=None, task_type=None, mode=None, priority=None):
    ensure_workspace(workspace)
    try:
        depends_on = parse_dependency_ids(depends_on)
        acceptance_criteria = parse_acceptance_criteria(acceptance_criteria)
        requested_task_type = task_type
        task_mode = "tasks" if requested_task_type is not None else "implementation"
        pipeline_mode = mode or "full"
        task = create_task(
            workspace,
            title=title,
            depends_on=None if depends_on is ... else depends_on,
            mode=task_mode,
            pipeline_mode=pipeline_mode,
            goal=goal,
            acceptance_criteria=None if acceptance_criteria is ... else acceptance_criteria,
            task_type=requested_task_type,
            priority=priority,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"add failed: {exc}")
        return 1
    print(
        f"Created task {task.id} in {workspace / '.litehive' / 'tasks' / (task.id + '-' + task.slug)}"
    )
    print(f"priority: {task.priority}")
    print(
        f"retry_limit: {task.retry_policy.max_retries if task.retry_policy.max_retries is not None else 'default'}"
    )
    print(f"priority: {task.priority}")
    print(f"mode: {task.mode}")
    print(f"pipeline_mode: {task.pipeline_mode}")
    print(f"model: {task_model_label(task.model)}")
    print("human_checkpoints: " + (", ".join(task.human_checkpoints) if task.human_checkpoints else "-"))
    print(f"task_type: {task.task_type or '-'}")
    print(f"depends_on: {task_dependencies_label(task.id, task.depends_on)}")
    print(f"pm_complexity: {task.pm_complexity or '-'}")
    print(f"planned_effort: {task.planned_effort or '-'}")
    print(f"acceptance_criteria: {len(task.acceptance_criteria)}")
    missing_criteria_reason = missing_acceptance_criteria_cli_warning(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


def cmd_issue(
    workspace,
    upstream,
    issue_type="runtime_bug",
    details="",
    acceptance_criteria=None,
    source_task=None,
    source_stage=None,
    source_role="recovery",
    source_project=None,
    litehive_workspace=None,
    patch_branch=None,
    patch_base="HEAD",
    prepare_patch_branch=False,
):
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
    source_task = None
    if source_task_id is not None:
        try:
            source_task = require_task(workspace, source_task_id)
        except ValueError as exc:
            print(f"issue failed: {exc}")
            return 1

    source_project = source_project or workspace_project_name(workspace)
    source_stage = source_stage or (
        source_task.pipeline_status if source_task is not None else None
    )
    source_title = source_task.title if source_task is not None else None
    patch = None
    if patch_branch:
        patch = UpstreamPatchProposal(
            branch=patch_branch,
            base_ref=patch_base,
            prepared=False,
            repo_path=str(litehive_root),
        )
        if prepare_patch_branch:
            try:
                patch = prepare_patch_branch_for_issue(
                    litehive_root,
                    branch=patch_branch,
                    base_ref=patch_base,
                )
            except ValueError as exc:
                print(f"issue failed: {exc}")
                return 1
    elif prepare_patch_branch:
        print("issue failed: --prepare-patch-branch requires --patch-branch")
        return 1

    contribution_kind = issue_type
    mode = (
        "tasks"
        if contribution_kind in {"missing_feature", "config_improvement", "prompt_improvement"}
        else "implementation"
    )
    task_type = (
        "bugfix"
        if contribution_kind == "runtime_bug"
        else "adapter"
        if contribution_kind == "engine_adapter_fix"
        else None
    )
    details = details.strip()
    goal_lines = [
        f"Upstream contribution from `{source_project}`.",
        f"Contribution kind: `{contribution_kind}`.",
    ]
    if source_task_id:
        goal_lines.append(f"Originating task: `{source_task_id}`.")
    if details:
        goal_lines.extend(["", details])
    goal = "\n".join(goal_lines)

    try:
        task = create_task(
            litehive_root,
            title=upstream,
            mode=mode,
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


def cmd_intake(file, workspace, engine="opencode", model=None):
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
    engine = get_engine(engine_name)
    model = model or (config.opencode_model if engine_name == "opencode" else None)

    prompt = intake_prompt(brain_dump)
    print(f"Analyzing brain dump with {engine_name}...")

    raw_title = fallback_intake_title(brain_dump)
    raw_goal = ""

    try:
        execution = engine.run(prompt, cwd=workspace, model=model)
        if execution.exit_code == 0:
            transcript = engine.render_transcript(execution)
            import re as _re

            _title_m = _re.search(r"^TITLE:\s*(.+)$", transcript, _re.MULTILINE)
            extracted_title = _title_m.group(1).strip() if _title_m else None
            _goal_m = _re.search(r"^GOAL:\s*(.+)$", transcript, _re.MULTILINE)
            extracted_goal = _goal_m.group(1).strip() if _goal_m else None

            if extracted_title:
                raw_title = extracted_title
            if extracted_goal:
                raw_goal = extracted_goal
        else:
            print(
                f"Warning: Analysis failed with exit code {execution.exit_code}. "
                "Creating task from raw intake."
            )
    except Exception as exc:
        print(f"Warning: Analysis failed ({exc}). Creating task from raw intake.")

    task = None
    try:
        from litehive.tasks.paths import task_dir
        from litehive.tasks.templates import task_brief_file

        task_goal = raw_goal.strip() if raw_goal.strip() else fallback_intake_goal(brain_dump)
        task_goal += "\n\n(See intake.md for the original brain dump)"

        task = create_task(
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


def cmd_update(
    task_id,
    workspace,
    title=None,
    priority=None,
    goal=None,
    depends_on=None,
    acceptance_criteria=None,
    constraint=None,
    plan_step=None,
    from_file=None,
    edit: bool = False,
):
    from litehive.cli.agent_cli import block_if_agent

    block_if_agent()
    ensure_workspace(workspace)
    rich_file = from_file
    edit_mode = edit
    if rich_file is not None and edit_mode:
        print("update failed: use either --from-file or --edit, not both")
        return 1
    if (
        title is None
        and depends_on is None
        and acceptance_criteria is None
        and constraint is None
        and plan_step is None
        and priority is None
        and goal is None
        and rich_file is None
        and not edit_mode
    ):
        print("update failed: no changes requested")
        return 1
    try:
        rich_updates = {}
        if rich_file is not None:
            rich_updates = load_rich_task_update_file(rich_file)
        elif edit_mode:
            rich_updates = collect_editor_task_updates(workspace, task_id)

        depends_on = parse_dependency_ids(
            depends_on, task_id=task_id, allow_clear=True
        )
        acceptance_criteria = parse_acceptance_criteria(
            acceptance_criteria,
            allow_clear=True,
        )
        constraints = parse_text_list_option(
            constraint,
            option_name="Constraints",
            allow_clear=True,
        )
        plan = parse_text_list_option(
            plan_step,
            option_name="Plan steps",
            allow_clear=True,
        )
        flag_updates = {}
        if depends_on is not ...:
            flag_updates["depends_on"] = depends_on
        if acceptance_criteria is not ...:
            flag_updates["acceptance_criteria"] = acceptance_criteria
        if constraints is not ...:
            flag_updates["constraints"] = constraints
        if plan is not ...:
            flag_updates["plan"] = plan
        if priority is not None:
            flag_updates["priority"] = priority
        if title is not None:
            flag_updates["title"] = title
        if goal is not None:
            flag_updates["goal"] = goal

        updates = merge_task_updates(rich_updates, flag_updates, overlay_source="CLI flags")
        task = update_task_metadata(
            workspace,
            task_id,
            title=updates.get("title", ...),
            depends_on=updates.get("depends_on", ...),
            task_type=updates.get("task_type", ...),
            model=updates.get("model", ...),
            retry_limit=updates.get("retry_limit", ...),
            priority=updates.get("priority", ...),
            pm_complexity=updates.get("pm_complexity", ...),
            planned_effort=updates.get("planned_effort", ...),
            goal=updates.get("goal", ...),
            acceptance_criteria=updates.get("acceptance_criteria", ...),
            constraints=updates.get("constraints", ...),
            plan=updates.get("plan", ...),
            human_checkpoints=updates.get("human_checkpoints", ...),
            mode=updates.get("mode", ...),
            auto_commit=updates.get("auto_commit", ...),
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"update failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print(f"model: {task_model_label(task.model)}")
    print(
        f"retry_limit: {task.retry_policy.max_retries if task.retry_policy.max_retries is not None else 'default'}"
    )
    print(f"priority: {task.priority}")
    print(f"pm_complexity: {task.pm_complexity or '-'}")
    print(f"planned_effort: {task.planned_effort or '-'}")
    print(f"mode: {task.mode}")
    print(f"auto_commit: {task.git.auto_commit}")
    print(
        "human_checkpoints: "
        + (", ".join(task.human_checkpoints) if task.human_checkpoints else "-")
    )
    print(f"task_type: {task.task_type or '-'}")
    print(f"depends_on: {task_dependencies_label(task.id, task.depends_on)}")
    print(f"goal: {task.goal}")
    print(f"acceptance_criteria: {len(task.acceptance_criteria)}")
    print(f"constraints: {len(task.constraints)}")
    print(f"plan: {len(task.plan)}")
    missing_criteria_reason = missing_acceptance_criteria_cli_warning(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0
