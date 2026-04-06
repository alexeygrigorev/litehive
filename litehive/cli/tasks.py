from litehive.config import ensure_workspace, load_config
from litehive.engines import get_engine
from litehive.models import UpstreamContributionOrigin, UpstreamPatchProposal
from litehive.subagents import intake_prompt
from litehive.tasks import (
    WorkspaceConflictError,
    create_task,
    discard_created_task,
    load_state,
    missing_acceptance_criteria_cli_warning,
    require_task,
    update_task_metadata,
)

from litehive.cli._display import (
    _fallback_intake_goal,
    _fallback_intake_title,
    _link_intake_brief_to_source,
    _prepare_patch_branch,
    _resolve_litehive_source_root,
    _task_dependencies_label,
    _task_engine_label,
    _task_model_label,
    _workspace_project_name,
)
from litehive.cli._parse import (
    _collect_editor_task_updates,
    _load_rich_task_update_file,
    _merge_task_updates,
    _parse_acceptance_criteria,
    _parse_dependency_ids,
    _parse_human_checkpoints,
    _parse_text_list_option,
)


def _cmd_add(args):
    ensure_workspace(args.workspace)
    try:
        depends_on = _parse_dependency_ids(getattr(args, "depends_on", None))
        acceptance_criteria = _parse_acceptance_criteria(getattr(args, "acceptance_criteria", None))
        human_checkpoints = _parse_human_checkpoints(getattr(args, "human_checkpoint", None))
        requested_task_type = getattr(args, "task_type", None)
        requested_mode = getattr(args, "mode", None)
        mode = requested_mode or ("tasks" if requested_task_type is not None else "implementation")
        pipeline_mode = "single" if getattr(args, "single", False) else "full"
        task = create_task(
            args.workspace,
            title=args.title,
            depends_on=None if depends_on is ... else depends_on,
            mode=mode,
            pipeline_mode=pipeline_mode,
            goal=args.goal,
            acceptance_criteria=None if acceptance_criteria is ... else acceptance_criteria,
            pm_complexity=getattr(args, "pm_complexity", None),
            planned_effort=getattr(args, "planned_effort", None),
            human_checkpoints=None if human_checkpoints is ... else human_checkpoints,
            task_type=requested_task_type,
            engine=args.engine,
            model=getattr(args, "model", None),
            retry_limit=getattr(args, "retry_limit", None),
            auto_commit=not args.no_auto_commit,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"add failed: {exc}")
        return 1
    print(
        f"Created task {task.id} in {args.workspace / '.litehive' / 'tasks' / (task.id + '-' + task.slug)}"
    )
    print(
        f"retry_limit: {task.retry_policy.max_retries if task.retry_policy.max_retries is not None else 'default'}"
    )
    print(f"mode: {task.mode}")
    print(f"pipeline_mode: {task.pipeline_mode}")
    print(f"engine: {_task_engine_label(task.engine, load_config(args.workspace).default_engine)}")
    print(f"model: {_task_model_label(task.model)}")
    print(
        "human_checkpoints: "
        + (", ".join(task.human_checkpoints) if task.human_checkpoints else "-")
    )
    print(f"task_type: {task.task_type or '-'}")
    print(f"depends_on: {_task_dependencies_label(task.id, task.depends_on)}")
    print(f"pm_complexity: {task.pm_complexity or '-'}")
    print(f"planned_effort: {task.planned_effort or '-'}")
    print(f"acceptance_criteria: {len(task.acceptance_criteria)}")
    missing_criteria_reason = missing_acceptance_criteria_cli_warning(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


def _cmd_issue(args):
    ensure_workspace(args.workspace)
    try:
        litehive_root = _resolve_litehive_source_root(args)
        ensure_workspace(litehive_root)
        acceptance_criteria = _parse_acceptance_criteria(getattr(args, "acceptance_criteria", None))
    except ValueError as exc:
        print(f"issue failed: {exc}")
        return 1

    state = load_state(args.workspace)
    source_task_id = args.source_task or state.active_task_id
    source_task = None
    if source_task_id is not None:
        try:
            source_task = require_task(args.workspace, source_task_id)
        except ValueError as exc:
            print(f"issue failed: {exc}")
            return 1

    source_project = args.source_project or _workspace_project_name(args.workspace)
    source_stage = args.source_stage or (
        source_task.pipeline_status if source_task is not None else None
    )
    source_title = source_task.title if source_task is not None else None
    patch = None
    if args.patch_branch:
        patch = UpstreamPatchProposal(
            branch=args.patch_branch,
            base_ref=args.patch_base,
            prepared=False,
            repo_path=str(litehive_root),
        )
        if args.prepare_patch_branch:
            try:
                patch = _prepare_patch_branch(
                    litehive_root,
                    branch=args.patch_branch,
                    base_ref=args.patch_base,
                )
            except ValueError as exc:
                print(f"issue failed: {exc}")
                return 1
    elif args.prepare_patch_branch:
        print("issue failed: --prepare-patch-branch requires --patch-branch")
        return 1

    contribution_kind = args.type
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
    details = args.details.strip()
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
            title=args.upstream,
            mode=mode,
            task_type=task_type,
            goal=goal,
            acceptance_criteria=None if acceptance_criteria is ... else acceptance_criteria,
            upstream_origin=UpstreamContributionOrigin(
                source_project=source_project,
                source_workspace=str(args.workspace.resolve()),
                source_task_id=source_task_id,
                source_task_title=source_title,
                source_stage=source_stage,
                source_role=args.source_role,
                contribution_kind=contribution_kind,
                summary=args.upstream,
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


def _cmd_intake(args):
    ensure_workspace(args.workspace)
    brain_dump = ""
    if args.file:
        try:
            brain_dump = args.file.read_text(encoding="utf-8")
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

    config = load_config(args.workspace)
    engine_name = args.engine or config.default_engine
    engine = get_engine(engine_name)
    model = args.model or (config.opencode_model if engine_name == "opencode" else None)

    prompt = intake_prompt(brain_dump)
    print(f"Analyzing brain dump with {engine_name}...")

    raw_title = _fallback_intake_title(brain_dump)
    raw_goal = ""

    try:
        execution = engine.run(prompt, cwd=args.workspace, model=model)
        if execution.exit_code == 0:
            transcript = engine.render_transcript(execution)
            from litehive.engines.base import _extract_line

            extracted_title = _extract_line(transcript, "TITLE")
            extracted_goal = _extract_line(transcript, "GOAL")

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
        from litehive.tasks import task_dir, task_brief_file

        task_goal = raw_goal.strip() if raw_goal.strip() else _fallback_intake_goal(brain_dump)
        task_goal += "\n\n(See intake.md for the original brain dump)"

        task = create_task(
            args.workspace,
            title=raw_title,
            goal=task_goal,
            mode="tasks",
            task_type="intake",
            engine=args.engine,
            model=args.model,
        )
        base = task_dir(args.workspace, task)
        (base / "intake.md").write_text(brain_dump, encoding="utf-8")

        brief_file = task_brief_file(args.workspace, task)
        _link_intake_brief_to_source(brief_file)

    except Exception as exc:
        if task is not None:
            discard_created_task(args.workspace, task.id)
        print(f"Task creation failed: {exc}")
        return 1

    print(f"Created task {task.id}: {task.title}")
    if task.goal:
        print(f"Goal: {task.goal}")
    print(f"Original dump preserved at: {base / 'intake.md'}")
    return 0


def _cmd_update(args):
    ensure_workspace(args.workspace)
    retry_limit_arg = getattr(args, "retry_limit", None)
    rich_file = getattr(args, "from_file", None)
    edit_mode = bool(getattr(args, "edit", False))
    if rich_file is not None and edit_mode:
        print("update failed: use either --from-file or --edit, not both")
        return 1
    if (
        getattr(args, "depends_on", None) is None
        and getattr(args, "acceptance_criteria", None) is None
        and getattr(args, "constraint", None) is None
        and getattr(args, "plan_step", None) is None
        and getattr(args, "human_checkpoint", None) is None
        and getattr(args, "task_type", None) is None
        and getattr(args, "engine", None) is None
        and getattr(args, "model", None) is None
        and retry_limit_arg is None
        and getattr(args, "priority", None) is None
        and getattr(args, "pm_complexity", None) is None
        and getattr(args, "planned_effort", None) is None
        and getattr(args, "goal", None) is None
        and getattr(args, "mode", None) is None
        and args.auto_commit is None
        and rich_file is None
        and not edit_mode
    ):
        print("update failed: no changes requested")
        return 1
    try:
        rich_updates = {}
        if rich_file is not None:
            rich_updates = _load_rich_task_update_file(rich_file)
        elif edit_mode:
            rich_updates = _collect_editor_task_updates(args.workspace, args.task_id)

        depends_on = _parse_dependency_ids(
            getattr(args, "depends_on", None), task_id=args.task_id, allow_clear=True
        )
        acceptance_criteria = _parse_acceptance_criteria(
            getattr(args, "acceptance_criteria", None),
            allow_clear=True,
        )
        constraints = _parse_text_list_option(
            getattr(args, "constraint", None),
            option_name="Constraints",
            allow_clear=True,
        )
        plan = _parse_text_list_option(
            getattr(args, "plan_step", None),
            option_name="Plan steps",
            allow_clear=True,
        )
        human_checkpoints = _parse_human_checkpoints(
            getattr(args, "human_checkpoint", None),
            allow_clear=True,
        )
        if retry_limit_arg is None:
            retry_limit = ...
        elif retry_limit_arg == "default":
            retry_limit = None
        else:
            retry_limit = int(retry_limit_arg)
        flag_updates = {}
        if depends_on is not ...:
            flag_updates["depends_on"] = depends_on
        if acceptance_criteria is not ...:
            flag_updates["acceptance_criteria"] = acceptance_criteria
        if constraints is not ...:
            flag_updates["constraints"] = constraints
        if plan is not ...:
            flag_updates["plan"] = plan
        if human_checkpoints is not ...:
            flag_updates["human_checkpoints"] = human_checkpoints
        if getattr(args, "task_type", None) is not None:
            flag_updates["task_type"] = (
                None
                if getattr(args, "task_type", None) == "default"
                else getattr(args, "task_type", None)
            )
        if getattr(args, "engine", None) is not None:
            flag_updates["engine"] = None if args.engine == "default" else args.engine
        if getattr(args, "model", None) is not None:
            flag_updates["model"] = (
                None if getattr(args, "model", None) == "default" else getattr(args, "model", None)
            )
        if retry_limit is not ...:
            flag_updates["retry_limit"] = retry_limit
        if getattr(args, "priority", None) is not None:
            flag_updates["priority"] = args.priority
        if getattr(args, "pm_complexity", None) is not None:
            flag_updates["pm_complexity"] = (
                None
                if getattr(args, "pm_complexity", None) == "none"
                else getattr(args, "pm_complexity", None)
            )
        if getattr(args, "planned_effort", None) is not None:
            flag_updates["planned_effort"] = (
                None
                if getattr(args, "planned_effort", None) == "none"
                else getattr(args, "planned_effort", None)
            )
        if getattr(args, "goal", None) is not None:
            flag_updates["goal"] = args.goal
        if getattr(args, "mode", None) is not None:
            flag_updates["mode"] = args.mode
        if args.auto_commit is not None:
            flag_updates["auto_commit"] = args.auto_commit

        updates = _merge_task_updates(rich_updates, flag_updates, overlay_source="CLI flags")
        task = update_task_metadata(
            args.workspace,
            args.task_id,
            depends_on=updates.get("depends_on", ...),
            task_type=updates.get("task_type", ...),
            engine=updates.get("engine", ...),
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
    config = load_config(args.workspace)
    print(f"task: {task.id} {task.title}")
    print(f"engine: {_task_engine_label(task.engine, config.default_engine)}")
    print(f"model: {_task_model_label(task.model)}")
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
    print(f"depends_on: {_task_dependencies_label(task.id, task.depends_on)}")
    print(f"goal: {task.goal}")
    print(f"acceptance_criteria: {len(task.acceptance_criteria)}")
    print(f"constraints: {len(task.constraints)}")
    print(f"plan: {len(task.plan)}")
    missing_criteria_reason = missing_acceptance_criteria_cli_warning(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0
