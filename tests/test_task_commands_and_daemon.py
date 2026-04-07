from tests.workspace_helpers import *  # noqa: F401,F403

def test_add_command_persists_pm_sizing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Estimated task",
            goal="",
            pm_complexity="moderate",
            planned_effort="m",
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            engine=None,
            model=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.pm_complexity == "moderate"
    assert task.planned_effort == "m"
    assert "pm_complexity: moderate" in output
    assert "planned_effort: m" in output

def test_add_command_persists_task_type(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Review queue behavior",
            goal="",
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type="review",
            mode=None,
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    brief = (task_dir(tmp_path, task) / "brief.md").read_text(encoding="utf-8")
    assert task.mode == "tasks"
    assert task.task_type == "review"
    assert (
        task.goal
        == "Review the target change critically and produce an actionable decision with supporting evidence."
    )
    assert "## Template Guidance" in brief
    assert "mode: tasks" in output
    assert "task_type: review" in output

def test_add_command_can_force_implementation_mode_for_typed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Review queue behavior",
            goal="",
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type="review",
            mode="implementation",
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.mode == "implementation"
    assert task.task_type == "review"
    assert task.goal == ""
    assert not (task_dir(tmp_path, task) / "brief.md").exists()
    assert "mode: implementation" in output

def test_add_command_warns_when_large_task_lacks_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="Prerequisite")

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Large task",
            goal="Ship deterministic routing",
            acceptance_criteria=None,
            depends_on=[prerequisite.id],
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "acceptance_criteria: 0" in output
    assert (
        "warning: Structured acceptance criteria are required before implementation for larger tasks."
        in output
    )
    assert "this task has: dependencies, an explicit goal." in output
    assert "This task will stay in `grooming` until criteria are added." in output
    assert "Use `--acceptance-criteria` to persist at least one structured bullet." in output

def test_add_command_allows_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Active task")
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Waiting behind executor",
            goal="",
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Created task T-0002" in output
    assert load_state(tmp_path).queue == ["T-0001", "T-0002"]

def test_update_command_replaces_and_clears_dependencies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First prerequisite")
    second = create_task(tmp_path, title="Second prerequisite")
    task = create_task(tmp_path, title="Dependent task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=[first.id, f"{second.id},{first.id}"],
            acceptance_criteria=None,
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.depends_on == [first.id, second.id]
    assert f"depends_on: {first.id}, {second.id}" in output

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=["none"],
            acceptance_criteria=None,
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    cleared = get_task(tmp_path, task.id)
    assert cleared is not None
    assert cleared.depends_on == []
    assert "depends_on: -" in output

def test_update_command_replaces_and_clears_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="Prerequisite")
    task = create_task(
        tmp_path,
        title="Tune task",
        depends_on=[prerequisite.id],
        acceptance_criteria=["Old criterion"],
    )

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=["First criterion", "Second criterion"],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.acceptance_criteria == ["First criterion", "Second criterion"]
    assert "acceptance_criteria: 2" in output

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=["none"],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    cleared = get_task(tmp_path, task.id)
    assert cleared is not None
    assert cleared.acceptance_criteria == []
    assert "acceptance_criteria: 0" in output
    assert (
        "warning: Structured acceptance criteria are required before implementation for larger tasks."
        in output
    )
    assert "Use `--acceptance-criteria` to persist at least one structured bullet." in output

def test_update_command_supports_rich_task_shaping_from_yaml_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="Prerequisite")
    task = create_task(tmp_path, title="Shape task", task_type="review", mode="tasks")
    update_file = tmp_path / "task-shape.yaml"
    update_file.write_text(
        "\n".join(
            [
                "goal: |",
                "  Refine the queued review task with richer operator-authored shaping.",
                "  Keep the durable task record aligned with planner grooming.",
                "acceptance_criteria:",
                "  - The CLI persists rich shaping fields through the task record.",
                "  - Operators can refine the task later without editing YAML directly.",
                "constraints:",
                "  - Reuse the existing task record fields.",
                "  - Keep failed updates atomic.",
                "plan:",
                "  - Design the operator-facing shaping flow.",
                "  - Implement the supported CLI update path.",
                "pm_complexity: moderate",
                "planned_effort: m",
                "depends_on:",
                f"  - {prerequisite.id}",
                "human_checkpoints:",
                "  - before_acceptance",
                "priority: high",
                "auto_commit: false",
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            constraint=None,
            plan_step=None,
            human_checkpoint=None,
            task_type=None,
            engine=None,
            model=None,
            retry_limit=None,
            priority=None,
            pm_complexity=None,
            planned_effort=None,
            goal=None,
            mode=None,
            auto_commit=None,
            from_file=update_file,
            edit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.goal.startswith("Refine the queued review task")
    assert updated.acceptance_criteria == [
        "The CLI persists rich shaping fields through the task record.",
        "Operators can refine the task later without editing YAML directly.",
    ]
    assert updated.constraints == [
        "Reuse the existing task record fields.",
        "Keep failed updates atomic.",
    ]
    assert updated.plan == [
        "Design the operator-facing shaping flow.",
        "Implement the supported CLI update path.",
    ]
    assert updated.pm_complexity == "moderate"
    assert updated.planned_effort == "m"
    assert updated.depends_on == [prerequisite.id]
    assert updated.human_checkpoints == ["before_acceptance"]
    assert updated.priority == "high"
    assert updated.git.auto_commit is False
    brief = (task_dir(tmp_path, updated) / "brief.md").read_text(encoding="utf-8")
    assert "## Constraints" in brief
    assert "- Reuse the existing task record fields." in brief
    assert "## Plan" in brief
    assert "- Implement the supported CLI update path." in brief
    assert "acceptance_criteria: 2" in output
    assert "constraints: 2" in output
    assert "plan: 2" in output

def test_update_command_rejects_malformed_rich_task_update_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Shape task")
    update_file = tmp_path / "bad-task-shape.yaml"
    update_file.write_text("acceptance_criteria: not-a-list\n", encoding="utf-8")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            constraint=None,
            plan_step=None,
            human_checkpoint=None,
            task_type=None,
            engine=None,
            model=None,
            retry_limit=None,
            priority=None,
            pm_complexity=None,
            planned_effort=None,
            goal=None,
            mode=None,
            auto_commit=None,
            from_file=update_file,
            edit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "update failed: acceptance_criteria must be a YAML list of strings" in output
    unchanged = get_task(tmp_path, task.id)
    assert unchanged is not None
    assert unchanged.acceptance_criteria == []

def test_update_command_supports_rich_task_shaping_via_editor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Edit task")

    monkeypatch.setenv("EDITOR", "fake-editor")

    def fake_editor(argv: list[str], check: bool = False):  # type: ignore[no-untyped-def]
        edit_path = Path(argv[-1])
        edit_path.write_text(
            "\n".join(
                [
                    "goal: |",
                    "  Capture a larger task refresh through the editor flow.",
                    "acceptance_criteria:",
                    "  - The editor-backed update persists structured fields.",
                    "constraints:",
                    "  - Avoid direct task.yaml edits.",
                    "plan:",
                    "  - Open the editor template.",
                    "  - Persist the edited task record.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("litehive.cli._parse.subprocess.run", fake_editor)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            constraint=None,
            plan_step=None,
            human_checkpoint=None,
            task_type=None,
            engine=None,
            model=None,
            retry_limit=None,
            priority=None,
            pm_complexity=None,
            planned_effort=None,
            goal=None,
            mode=None,
            auto_commit=None,
            from_file=None,
            edit=True,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.goal == "Capture a larger task refresh through the editor flow."
    assert updated.acceptance_criteria == [
        "The editor-backed update persists structured fields."
    ]
    assert updated.constraints == ["Avoid direct task.yaml edits."]
    assert updated.plan == [
        "Open the editor template.",
        "Persist the edited task record.",
    ]
    assert "constraints: 1" in output
    assert "plan: 2" in output

def test_update_command_replaces_and_clears_pm_sizing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task", pm_complexity="simple", planned_effort="s")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            engine=None,
            model=None,
            retry_limit=None,
            priority=None,
            pm_complexity="complex",
            planned_effort="l",
            goal=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.pm_complexity == "complex"
    assert updated.planned_effort == "l"
    assert "pm_complexity: complex" in output
    assert "planned_effort: l" in output

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            engine=None,
            model=None,
            retry_limit=None,
            priority=None,
            pm_complexity="none",
            planned_effort="none",
            goal=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    cleared = get_task(tmp_path, task.id)
    assert cleared is not None
    assert cleared.pm_complexity is None
    assert cleared.planned_effort is None
    assert "pm_complexity: -" in output
    assert "planned_effort: -" in output

def test_update_command_warns_when_metadata_change_makes_task_require_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task")
    queued = get_task(tmp_path, task.id)
    assert queued is not None
    queued.pipeline_status = "testing"
    save_task(tmp_path, queued)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            engine=None,
            retry_limit=None,
            priority="high",
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.priority == "high"
    assert updated.pipeline_status == "grooming"
    assert (
        "warning: Structured acceptance criteria are required before implementation for larger tasks."
        in output
    )
    assert "Use `--acceptance-criteria` to persist at least one structured bullet." in output

def test_update_command_warns_when_goal_makes_task_require_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task")
    queued = get_task(tmp_path, task.id)
    assert queued is not None
    queued.pipeline_status = "testing"
    save_task(tmp_path, queued)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            engine=None,
            retry_limit=None,
            priority=None,
            goal="Ship deterministic routing",
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.goal == "Ship deterministic routing"
    assert updated.pipeline_status == "grooming"
    assert (
        "warning: Structured acceptance criteria are required before implementation for larger tasks."
        in output
    )
    assert "this task has: an explicit goal." in output
    assert "This task will stay in `grooming` until criteria are added." in output

def test_update_command_reroutes_large_task_missing_criteria_back_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="Prerequisite")
    task = create_task(
        tmp_path,
        title="Tune task",
        depends_on=[prerequisite.id],
        acceptance_criteria=["Existing criterion"],
    )
    queued = get_task(tmp_path, task.id)
    assert queued is not None
    queued.pipeline_status = "testing"
    save_task(tmp_path, queued)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=["none"],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.acceptance_criteria == []
    assert updated.pipeline_status == "grooming"
    assert "acceptance_criteria: 0" in output
    assert (
        "warning: Structured acceptance criteria are required before implementation for larger tasks."
        in output
    )

def test_update_command_preserves_later_stage_when_acceptance_gate_is_satisfied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Tune task",
        goal="Ship queue CLI",
        acceptance_criteria=["Existing criterion"],
    )
    queued = get_task(tmp_path, task.id)
    assert queued is not None
    queued.pipeline_status = "testing"
    save_task(tmp_path, queued)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            engine=None,
            retry_limit=None,
            priority="high",
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    capsys.readouterr()

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.pipeline_status == "testing"

def test_add_command_rejects_missing_dependency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Blocked task",
            goal="",
            acceptance_criteria=None,
            depends_on=["T-9999"],
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "add failed: Task T-9999 not found" in output

def test_update_command_rejects_dependency_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    first.depends_on = [second.id]
    save_task(tmp_path, first)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=second.id,
            depends_on=[first.id],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert f"update failed: Task {second.id} dependency cycle detected via {first.id}" in output

def test_move_command_reorders_queue(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    third = create_task(tmp_path, title="Third task")

    exit_code = _cmd_move(argparse.Namespace(workspace=tmp_path, task_id=third.id, position=1))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "position: 1" in output
    assert load_state(tmp_path).queue == [third.id, first.id, second.id]

def test_move_command_reorders_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    third = create_task(tmp_path, title="Third task")
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_move(argparse.Namespace(workspace=tmp_path, task_id=third.id, position=1))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "position: 1" in output
    assert load_state(tmp_path).queue == [third.id, first.id, second.id]

def test_add_command_creates_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    set_active_task(tmp_path, active.id)
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Pending task",
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            goal="",
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Created task T-0002" in output
    state = load_state(tmp_path)
    assert state.active_task_id == active.id
    assert state.queue == ["T-0002"]
    queued = get_task(tmp_path, "T-0002")
    assert queued is not None
    assert queued.status == "queued"
    assert queued.pipeline_status == "backlog"

def test_add_command_persists_task_model_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Pending task",
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            goal="",
            engine="gemini",
            model="gemini-2.5-pro",
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.engine == "gemini"
    assert task.model == "gemini-2.5-pro"
    assert "model: gemini-2.5-pro" in output

def test_add_command_persists_priority_high(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="High priority task",
            goal="",
            pm_complexity=None,
            planned_effort=None,
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            engine=None,
            model=None,
            retry_limit=None,
            no_auto_commit=False,
            priority="high",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.priority == "high"
    assert "priority: high" in output

def test_add_command_defaults_priority_to_medium(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Default priority task",
            goal="",
            pm_complexity=None,
            planned_effort=None,
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            engine=None,
            model=None,
            retry_limit=None,
            no_auto_commit=False,
            priority=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.priority == "medium"
    assert "priority: medium" in output

def test_add_command_persists_priority_critical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Critical priority task",
            goal="",
            pm_complexity=None,
            planned_effort=None,
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            engine=None,
            model=None,
            retry_limit=None,
            no_auto_commit=False,
            priority="critical",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.priority == "critical"
    assert "priority: critical" in output

def test_promote_command_moves_queued_task_to_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")

    exit_code = _cmd_promote(argparse.Namespace(workspace=tmp_path, task_id=second.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "position: 1" in output
    assert load_state(tmp_path).queue == [second.id, first.id]

def test_prioritize_command_reorders_future_tasks_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    third = create_task(tmp_path, title="Third task")
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_prioritize(
        argparse.Namespace(workspace=tmp_path, task_ids=[third.id, second.id])
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"moved_tasks: {third.id} {second.id}" in output
    assert "moved_count: 2" in output
    assert f"front_of_queue: {third.id} {second.id}" in output
    assert "queue_length: 3" in output
    assert load_state(tmp_path).queue == [third.id, second.id, first.id]

def test_prioritize_command_rejects_duplicate_task_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="First task")

    exit_code = _cmd_prioritize(argparse.Namespace(workspace=tmp_path, task_ids=[task.id, task.id]))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert f"prioritize failed: Task ids must be unique: {task.id}" in output
    assert load_state(tmp_path).queue == [task.id]

def test_prioritize_command_rejects_task_that_is_not_currently_queued(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending task")
    not_queued = create_task(tmp_path, title="Not queued task")
    task = get_task(tmp_path, not_queued.id)
    assert task is not None
    task.status = "flagged"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [queued.id]
    save_state(tmp_path, state)

    exit_code = _cmd_prioritize(
        argparse.Namespace(workspace=tmp_path, task_ids=[not_queued.id, queued.id])
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert f"prioritize failed: Tasks are not queued: {not_queued.id}" in output
    assert load_state(tmp_path).queue == [queued.id]

def test_update_command_allows_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    queued = create_task(tmp_path, title="Pending task")
    set_active_task(tmp_path, active.id)
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=queued.id,
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type=None,
            engine=None,
            retry_limit=None,
            priority="high",
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "priority: high" in output
    updated = get_task(tmp_path, queued.id)
    assert updated is not None
    assert updated.priority == "high"

def test_update_command_rejects_active_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    set_active_task(tmp_path, active.id)
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=active.id,
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type=None,
            engine=None,
            retry_limit=None,
            priority="high",
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert (
        "update failed: runner is actively using task state that cannot be changed concurrently"
        in output
    )

def test_promote_command_resumes_flagged_task_to_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Resume me first")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    exit_code = _cmd_promote(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: testing" in output
    assert load_state(tmp_path).queue == [flagged.id, first.id]
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "testing"
    assert resumed.runtime.execution_status == "idle"

def test_promote_command_warns_when_resumed_task_still_needs_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Needs criteria", goal="Ship queue CLI")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.plan = ["Inspect current flow", "Implement gate"]
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    exit_code = _cmd_promote(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: grooming" in output
    assert (
        "warning: Structured acceptance criteria are required before implementation for larger tasks."
        in output
    )
    assert "Use `--acceptance-criteria` to persist at least one structured bullet." not in output
    assert load_state(tmp_path).queue == [flagged.id, first.id]
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "grooming"
    assert resumed.runtime.execution_status == "idle"

def test_promote_command_resumes_flagged_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    flagged = create_task(tmp_path, title="Resume me first")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)
    set_active_task(tmp_path, active.id)
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_promote(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "position: 1" in output
    state = load_state(tmp_path)
    assert state.active_task_id == active.id
    assert state.queue[0] == flagged.id

def test_requeue_command_requeues_flagged_task_to_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Needs another pass")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    from litehive.tasks import save_task

    save_task(tmp_path, task)

    exit_code = _cmd_requeue_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=True)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: implementing" in output
    assert load_state(tmp_path).queue == [flagged.id, first.id]
    requeued = get_task(tmp_path, flagged.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.pipeline_status == "implementing"

def test_requeue_command_reroutes_large_task_without_acceptance_criteria_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Needs criteria", goal="Ship queue CLI")
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    flagged.status = "flagged"
    flagged.pipeline_status = "testing"
    flagged.plan = ["Inspect current flow", "Implement gate"]
    save_task(tmp_path, flagged)

    exit_code = _cmd_requeue_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "pipeline_status: grooming" in output
    assert (
        "warning: Structured acceptance criteria are required before implementation for larger tasks."
        in output
    )
    requeued = get_task(tmp_path, flagged.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.pipeline_status == "grooming"

def test_requeue_command_restarts_parked_task_from_implementation_entry_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    parked = create_task(tmp_path, title="Parked task")
    task = get_task(tmp_path, parked.id)
    assert task is not None
    task.status = "parked"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "interrupted"
    task.runtime.last_outcome.kind = "interrupted"
    task.runtime.last_outcome.stage = "testing"
    task.runtime.last_outcome.reason_code = "execution_interrupted"
    task.runtime.last_outcome.reason = "Task stopped via CLI."
    save_task(tmp_path, task)

    exit_code = _cmd_requeue_task(
        argparse.Namespace(workspace=tmp_path, task_id=parked.id, front=True)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: implementing" in output
    assert load_state(tmp_path).queue == [parked.id, first.id]
    requeued = get_task(tmp_path, parked.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.pipeline_status == "implementing"
    assert requeued.runtime.last_outcome.kind == "interrupted"

def test_resume_command_preserves_flagged_task_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Resume later")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "accepting"
    task.runtime.execution_status = "flagged"
    task.runtime.last_outcome.kind = "flagged"
    task.runtime.last_outcome.stage = "accepting"
    task.runtime.last_outcome.reason_code = "verdict_fail"
    task.runtime.last_outcome.reason = "accepting failed"
    save_task(tmp_path, task)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: accepting" in output
    assert load_state(tmp_path).queue == [first.id, flagged.id]
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "accepting"
    assert resumed.runtime.execution_status == "idle"
    assert resumed.runtime.last_outcome.kind == "flagged"
    assert resumed.runtime.last_outcome.stage == "accepting"

def test_resume_command_preserves_interrupted_task_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    interrupted = create_task(tmp_path, title="Resume interrupted task")
    task = get_task(tmp_path, interrupted.id)
    assert task is not None
    task.status = "interrupted"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "interrupted"
    task.runtime.last_outcome.kind = "interrupted"
    task.runtime.last_outcome.stage = "testing"
    task.runtime.last_outcome.reason_code = "execution_interrupted"
    task.runtime.last_outcome.reason = "Interrupted run recovered. Resume from `testing`."
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="interrupted",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="blocked",
        summary="Interrupted run recovered. Resume from `testing`.",
    )
    task.runtime.last_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="interrupted",
        path="subagents/SA-0001-qa",
        pid=4242,
        started_at="2026-04-01T00:00:10+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        transcript_snippet="tests were halfway done",
    )
    save_task(tmp_path, task)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=interrupted.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: testing" in output
    assert load_state(tmp_path).queue == [first.id, interrupted.id]
    resumed = get_task(tmp_path, interrupted.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "testing"
    assert resumed.runtime.execution_status == "idle"
    assert resumed.runtime.current_stage.step is None
    assert resumed.runtime.current_stage.status == "idle"
    assert resumed.runtime.last_outcome.kind == "interrupted"
    assert resumed.runtime.last_outcome.stage == "testing"
    assert resumed.runtime.last_outcome.reason_code == "execution_interrupted"
    assert resumed.runtime.last_subagent is not None
    assert resumed.runtime.last_subagent.transcript_snippet == "tests were halfway done"

def test_resume_command_preserves_parked_task_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    parked = create_task(tmp_path, title="Resume parked task")
    task = get_task(tmp_path, parked.id)
    assert task is not None
    task.status = "parked"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "interrupted"
    task.runtime.last_outcome.kind = "interrupted"
    task.runtime.last_outcome.stage = "testing"
    task.runtime.last_outcome.reason_code = "execution_interrupted"
    task.runtime.last_outcome.reason = "Task stopped via CLI."
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="interrupted",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="blocked",
        summary="Execution interrupted via `litehive stop`. Resume from `testing`.",
    )
    save_task(tmp_path, task)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=parked.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: testing" in output
    assert load_state(tmp_path).queue == [first.id, parked.id]
    resumed = get_task(tmp_path, parked.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "testing"
    assert resumed.runtime.execution_status == "idle"
    assert resumed.runtime.last_outcome.kind == "interrupted"
    assert resumed.runtime.last_outcome.stage == "testing"

def test_resume_run_uses_structured_continuation_handoff_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(auto_commit=False))
    create_task(tmp_path, title="Resume with structured handoff", auto_commit=False)
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-01T00:00:00+00:00"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:20+00:00",
    )
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="gemini",
        status="running",
        path="subagents/SA-0001-qa",
        pid=5151,
        started_at="2026-04-01T00:00:05+00:00",
        updated_at="2026-04-01T00:00:20+00:00",
        transcript_snippet="tests were halfway done",
        continuation=RuntimeEngineContinuation(session_id="gemini_resume_123"),
    )
    save_task(tmp_path, task)

    tasks_module._prepare_interrupted_task(
        tmp_path,
        task,
        stage="testing",
        summary="Runner stopped mid-testing.",
        reason="Runner stopped mid-testing.",
    )
    save_task(tmp_path, task)
    resumed = tasks_module.resume_task(tmp_path, task.id, front=True)
    assert resumed.runtime.continuation_handoff is not None
    assert resumed.runtime.continuation_handoff.kind == "restart"

    prompts: list[str] = []

    def fake_run(self, current_task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if current_task.pipeline_status == "testing":
            prompts.append(prompt)
        return _completed_subagent_result(
            tmp_path, current_task.pipeline_status, engine_name=engine_name, task=current_task
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status in {"done", "queued"}
    assert len(prompts) == 1
    assert "Continuation handoff:" in prompts[0]
    assert "- Kind: restart" in prompts[0]
    assert "- Engine path: gemini -> gemini" in prompts[0]
    assert "- Prior subagent: SA-0001 at `subagents/SA-0001-qa`" in prompts[0]
    assert "- Engine session id: gemini_resume_123" in prompts[0]
    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.runtime.continuation_handoff is None

def test_resume_command_preserves_flagged_commit_to_git_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Resume commit")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "commit_to_git"
    task.runtime.execution_status = "flagged"
    task.runtime.last_outcome.kind = "flagged"
    task.runtime.last_outcome.stage = "commit_to_git"
    task.runtime.last_outcome.reason_code = "stage_exception"
    task.runtime.last_outcome.reason = "commit failed"
    save_task(tmp_path, task)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: commit_to_git" in output
    assert load_state(tmp_path).queue == [first.id, flagged.id]
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "commit_to_git"
    assert resumed.runtime.execution_status == "idle"
    assert resumed.runtime.last_outcome.kind == "flagged"
    assert resumed.runtime.last_outcome.stage == "commit_to_git"

def test_resume_command_reroutes_large_task_missing_criteria_from_implementing_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume later", goal="Ship queue CLI")
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    flagged.status = "flagged"
    flagged.pipeline_status = "implementing"
    flagged.runtime.execution_status = "flagged"
    flagged.plan = ["Inspect current flow", "Implement gate"]
    save_task(tmp_path, flagged)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "pipeline_status: grooming" in output
    assert (
        "warning: Structured acceptance criteria are required before implementation for larger tasks."
        in output
    )
    assert "Use `--acceptance-criteria` to persist at least one structured bullet." not in output
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "grooming"
    assert resumed.runtime.execution_status == "idle"

def test_resume_command_reroutes_large_task_missing_criteria_from_later_stage_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume later", goal="Ship queue CLI")
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    flagged.status = "flagged"
    flagged.pipeline_status = "accepting"
    flagged.runtime.execution_status = "flagged"
    flagged.plan = ["Inspect current flow", "Implement gate"]
    save_task(tmp_path, flagged)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "pipeline_status: grooming" in output
    assert (
        "warning: Structured acceptance criteria are required before implementation for larger tasks."
        in output
    )
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "grooming"
    assert resumed.runtime.execution_status == "idle"

def test_requeue_command_requires_flagged_or_cancelled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Still queued")

    exit_code = _cmd_requeue_task(
        argparse.Namespace(workspace=tmp_path, task_id=task.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "is not flagged, parked, or closed" in output

def test_requeue_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Retry me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        requeue_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "testing"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task requeued for another implementation pass." not in journal

def test_requeue_task_rolls_back_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Retry me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        requeue_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "flagged"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task requeued for another implementation pass." not in journal

def test_resume_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Resume me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "accepting"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        resume_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "accepting"
    assert refreshed.runtime.execution_status == "flagged"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task resumed from `accepting`." not in journal

def test_resume_task_rolls_back_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Resume me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "accepting"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        resume_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "accepting"
    assert refreshed.runtime.execution_status == "flagged"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task resumed from `accepting`." not in journal

def test_requeue_task_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Retry me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        requeue_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "flagged"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task requeued for another implementation pass." not in journal

def test_resume_task_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Resume me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "accepting"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        resume_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "accepting"
    assert refreshed.runtime.execution_status == "flagged"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task resumed from `accepting`." not in journal

def test_abandon_command_cancels_task_and_removes_it_from_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Stop this task")
    queued = create_task(tmp_path, title="Keep this task")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    exit_code = _cmd_abandon_task(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: cancelled" in output
    assert "pipeline_status: testing" in output
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id]
    abandoned = get_task(tmp_path, flagged.id)
    assert abandoned is not None
    assert abandoned.status == "cancelled"
    assert abandoned.runtime.execution_status == "cancelled"
    journal = (
        tmp_path / ".litehive" / "tasks" / f"{abandoned.id}-{abandoned.slug}" / "journal.md"
    ).read_text(encoding="utf-8")
    assert "Task abandoned via CLI at stage `testing`." in journal

def test_abandon_command_allows_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    flagged = create_task(tmp_path, title="Stop this later")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)
    set_active_task(tmp_path, active.id)
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_abandon_task(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: cancelled" in output
    state = load_state(tmp_path)
    assert state.active_task_id == active.id
    assert flagged.id not in state.queue
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "cancelled"

def test_abandon_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Stop this task")
    queued = create_task(tmp_path, title="Keep this task")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        abandon_task(tmp_path, flagged.id)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.runtime.execution_status != "cancelled"
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert restored_state.queue == [flagged.id, queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task abandoned via CLI at stage `testing`." not in journal

def test_abandon_task_rolls_back_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Stop this task")
    queued = create_task(tmp_path, title="Keep this task")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        abandon_task(tmp_path, flagged.id)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.runtime.execution_status == "flagged"
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert restored_state.queue == [flagged.id, queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task abandoned via CLI at stage `testing`." not in journal

def test_abandon_task_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Stop this task")
    queued = create_task(tmp_path, title="Keep this task")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        abandon_task(tmp_path, flagged.id)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.runtime.execution_status == "flagged"
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert restored_state.queue == [flagged.id, queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task abandoned via CLI at stage `testing`." not in journal

def test_stop_command_interrupts_active_task_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Stop active task", auto_commit=False)
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "running"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at=None,
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict=None,
        summary="",
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    exit_code = _cmd_stop_task(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"task: {task.id} {task.title}" in output
    assert "status: parked" in output
    assert "pipeline_status: testing" in output
    assert "signal_sent: no" in output
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "parked"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert load_state(tmp_path).active_task_id is None
    assert load_state(tmp_path).queue == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `testing` was running." in journal
    assert "Reason: Task stopped via CLI." in journal
    assert "Resume from `testing`." in journal

def test_stop_current_task_requeues_commit_stage_interrupt(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Stop commit task", auto_commit=False)
    task.status = "in_progress"
    task.pipeline_status = "commit_to_git"
    task.runtime.execution_status = "running"
    task.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at=None,
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict=None,
        summary="",
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    summary = stop_current_task(tmp_path)

    assert summary.signal_sent is False
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert load_state(tmp_path).active_task_id is None
    assert load_state(tmp_path).queue == [task.id]

def test_stop_current_task_signals_live_runner_before_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Signal active task", auto_commit=False)
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "running"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at=None,
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict=None,
        summary="",
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    held_states = iter([True, False])
    signals: list[tuple[int, int]] = []

    monkeypatch.setattr(
        "litehive.tasks._runner_lock_is_held", lambda root: next(held_states, False)
    )
    monkeypatch.setattr(
        "litehive.tasks._read_runner_lock_metadata",
        lambda root: RunnerStatusState(pid=4242, started_at="2026-04-01T00:00:00+00:00"),
    )
    monkeypatch.setattr("litehive.tasks._runner_pid_is_alive", lambda pid: True)
    monkeypatch.setattr("litehive.tasks.recover_stale_runner_state", lambda root: False)

    def fake_kill(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr("litehive.tasks.os.kill", fake_kill)

    summary = stop_current_task(tmp_path, wait_timeout_seconds=0.01, poll_interval_seconds=0.01)

    assert signals == [(4242, tasks_module.signal.SIGINT)]
    assert summary.signal_sent is True
    assert summary.runner_pid == 4242
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "parked"
    assert refreshed.runtime.execution_status == "interrupted"

def test_switch_command_interrupts_active_task_persists_engine_and_records_thread_comment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    task = create_task(tmp_path, title="Switch active task", auto_commit=False)
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-01T00:00:00+00:00"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at=None,
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict=None,
        summary="",
    )
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="running",
        path="subagents/SA-0001-qa",
        pid=5151,
        started_at="2026-04-01T00:00:05+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        transcript_snippet="tests were halfway done",
        continuation=RuntimeEngineContinuation(session_id="codex_resume_123"),
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)
    (task_dir(tmp_path, task) / "subagents" / "SA-0001-qa").mkdir(parents=True)

    set_active_task(tmp_path, task.id)

    exit_code = _cmd_switch_task(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="gemini",
            reason="codex quota exhausted",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: testing" in output
    assert "engine: codex -> gemini" in output
    assert "was_active: yes" in output
    assert "position: 1" in output

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.engine == "gemini"
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "idle"
    assert refreshed.runtime.active_subagent is None
    assert refreshed.runtime.continuation_handoff is not None
    assert refreshed.runtime.continuation_handoff.kind == "restart"
    assert refreshed.runtime.continuation_handoff.subagent_path == "subagents/SA-0001-qa"
    assert refreshed.runtime.last_engine_switch is not None
    assert refreshed.runtime.last_engine_switch.from_engine == "codex"
    assert refreshed.runtime.last_engine_switch.to_engine == "gemini"
    assert refreshed.runtime.last_engine_switch.reason == "codex quota exhausted"
    assert load_state(tmp_path).active_task_id is None
    assert load_state(tmp_path).queue == [task.id]

    thread = tasks_module.load_task_thread(tmp_path, refreshed)
    assert thread[-1].role == "operator"
    assert "Engine switch requested: codex quota exhausted" in thread[-1].message
    assert "engine: codex -> gemini" in thread[-1].message
    assert "resume_from: testing" in thread[-1].message
    assert "- subagents/SA-0001-qa" in thread[-1].message
    assert "- subagents/SA-0001-qa/transcript.md" in thread[-1].message

def test_switch_task_engine_resumes_interrupted_task_at_same_stage_and_front_of_queue(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    first = create_task(tmp_path, title="Keep first queued", auto_commit=False)
    interrupted = create_task(tmp_path, title="Switch interrupted task", auto_commit=False)
    interrupted.status = "interrupted"
    interrupted.pipeline_status = "implementing"
    interrupted.engine = "codex"
    interrupted.runtime.execution_status = "interrupted"
    interrupted.runtime.last_outcome.kind = "interrupted"
    interrupted.runtime.last_outcome.stage = "implementing"
    interrupted.runtime.last_outcome.reason_code = "execution_interrupted"
    interrupted.runtime.last_subagent = RuntimeSubagentState(
        id="SA-0002",
        role="swe",
        engine="codex",
        status="interrupted",
        path="subagents/SA-0002-swe",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:30+00:00",
        completed_at="2026-04-01T00:00:30+00:00",
        transcript_snippet="implementation half done",
    )
    interrupted.runtime.continuation_handoff = RuntimeContinuationHandoff(
        step="implementing",
        kind="restart",
        reason="Need a different engine",
        from_engine="codex",
        to_engine="codex",
        subagent_id="SA-0002",
        subagent_path="subagents/SA-0002-swe",
        status="interrupted",
        summary="implementation half done",
        transcript_snippet="implementation half done",
        warnings=[],
        transcript_path="subagents/SA-0002-swe/transcript.md",
        updated_at="2026-04-01T00:00:30+00:00",
    )
    save_task(tmp_path, interrupted)
    save_task_runtime(tmp_path, interrupted)
    (task_dir(tmp_path, interrupted) / "subagents" / "SA-0002-swe").mkdir(parents=True)

    switched = switch_task_engine(
        tmp_path,
        interrupted.id,
        engine="gemini",
        reason="Need larger context window",
    )

    assert switched.was_active is False
    assert switched.previous_engine == "codex"
    assert switched.new_engine == "gemini"
    assert load_state(tmp_path).queue == [interrupted.id, first.id]

    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.engine == "gemini"
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert refreshed.runtime.continuation_handoff is not None
    assert refreshed.runtime.continuation_handoff.subagent_path == "subagents/SA-0002-swe"

    thread = tasks_module.load_task_thread(tmp_path, refreshed)
    assert "Engine switch requested: Need larger context window" in thread[-1].message
    assert "engine: codex -> gemini" in thread[-1].message
    assert "resume_from: implementing" in thread[-1].message
    assert "- subagents/SA-0002-swe" in thread[-1].message

def test_runner_flags_task_when_retry_limit_exhausted(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Exhausted task")
    # max_retries=1 allows 1 retry; on the 2nd rejection (rejections > 1) the task is flagged
    task.retry_policy.max_retries = 1
    save_task(tmp_path, task)
    rejection_count = {"n": 0}

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            rejection_count["n"] += 1
            verdict = "fail"
        else:
            verdict = "pass"
        return StageReport(task_id=task.id, step=step, verdict=verdict, summary=f"{step} {verdict}", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=1)
    # First run: testing fails → requeued (1 rejection allowed)
    result1 = runner.run(task)
    assert result1.final_status == "queued"
    finish_task_run_transition(tmp_path, task, result1.final_status)
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.retry_count == 1

    # Second run: testing fails again → retry limit exceeded → flagged
    result2 = runner.run(refreshed)
    assert result2.final_status == "flagged"
    finish_task_run_transition(tmp_path, refreshed, result2.final_status)
    refreshed2 = get_task(tmp_path, task.id)
    assert refreshed2 is not None
    assert refreshed2.runtime.execution_status == "flagged"
    assert refreshed2.status == "flagged"
    assert refreshed2.runtime.retry_count == 2
    assert refreshed2.runtime.retry_limit == 1
    assert refreshed2.runtime.last_outcome.kind == "flagged"
    assert refreshed2.runtime.last_outcome.reason_code == "retry_limit_exhausted"

@pytest.mark.parametrize("outcome", ["wont_do", "deferred", "duplicate"])
def test_close_task_non_implementation_outcomes(tmp_path: Path, outcome: str) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Close me")
    follow_up = create_task(tmp_path, title="Follow up later")
    state = load_state(tmp_path)
    assert task.id in state.queue

    closed = close_task(
        tmp_path,
        task.id,
        outcome=outcome,
        reason="Test reason",
        follow_up_task_id=follow_up.id,
    )

    assert closed.status == outcome
    assert closed.runtime.last_outcome.kind == outcome
    assert closed.runtime.last_outcome.reason_code == outcome
    assert closed.runtime.last_outcome.reason == "Test reason"
    assert closed.runtime.last_outcome.follow_up_task_id == follow_up.id
    state = load_state(tmp_path)
    assert task.id not in state.queue
    journal = (task_dir(tmp_path, closed) / "journal.md").read_text(encoding="utf-8")
    assert f"Task closed: {outcome}." in journal
    assert f"Follow-up task: {follow_up.id}." in journal

def test_close_task_rolls_back_when_atomic_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Close me")

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        close_task(tmp_path, task.id, outcome="deferred", reason="Not now")

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.runtime.last_outcome.reason_code is None
    assert load_state(tmp_path).queue == [task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task closed: deferred." not in journal

def test_close_task_rolls_back_when_atomic_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Close me")

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        close_task(tmp_path, task.id, outcome="deferred", reason="Not now")

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.runtime.last_outcome.reason_code is None
    assert load_state(tmp_path).queue == [task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task closed: deferred." not in journal

def test_cmd_close_task_wont_do(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Will not implement")
    follow_up = create_task(tmp_path, title="Track replacement")

    exit_code = _cmd_close_task(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            outcome="wont_do",
            reason=None,
            follow_up_task=follow_up.id,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: wont_do" in output
    assert "outcome: wont_do" in output
    assert f"follow_up_task: {follow_up.id}" in output
    closed = get_task(tmp_path, task.id)
    assert closed is not None
    assert closed.status == "wont_do"
    assert closed.runtime.last_outcome.kind == "wont_do"
    assert closed.runtime.last_outcome.reason_code == "wont_do"
    assert closed.runtime.last_outcome.follow_up_task_id == follow_up.id

def test_close_command_allows_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    queued = create_task(tmp_path, title="Won't do later")
    set_active_task(tmp_path, active.id)
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_close_task(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=queued.id,
            outcome="deferred",
            reason=None,
            follow_up_task=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: deferred" in output
    assert "outcome: deferred" in output
    state = load_state(tmp_path)
    assert state.active_task_id == active.id
    assert queued.id not in state.queue
    refreshed = get_task(tmp_path, queued.id)
    assert refreshed is not None
    assert refreshed.runtime.last_outcome.reason_code == "deferred"

def test_status_command_shows_explicit_close_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Defer me")
    follow_up = create_task(tmp_path, title="Future reconsideration")

    close_task(
        tmp_path,
        task.id,
        outcome="deferred",
        reason="Revisit after launch",
        follow_up_task_id=follow_up.id,
    )

    _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert f"{task.id} [deferred/backlog]" in output
    assert "outcome=deferred" in output
    assert "reason_code=deferred" in output
    assert f"follow_up_task={follow_up.id}" in output
    assert "reason=Revisit after launch" in output

def test_dirty_worktree_gate_reports_clean_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Clean task")
    _commit_repo_state(tmp_path)

    exit_code = _cmd_dirty_worktree_gate(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "dirty_worktree_gate: open" in output
    assert "clean: yes" in output
    assert "recorded task worktrees are clean" in output

def test_dirty_worktree_gate_reports_dirty_main_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Pending task")
    _commit_repo_state(tmp_path)
    (tmp_path / "app.txt").write_text("changed\n", encoding="utf-8")

    exit_code = _cmd_dirty_worktree_gate(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "dirty_worktree_gate: blocked" in output
    assert "location_kind: main-checkout" in output
    assert "ownership: main-checkout" in output
    assert "dirty_paths: app.txt" in output

def test_dirty_worktree_gate_reports_task_owned_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    task = create_task(tmp_path, title="Interrupted task")
    worktree_path = tmp_path.parent / "owned-worktree"
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    task.git.worktree_path = "../owned-worktree"
    save_task(tmp_path, task)
    _commit_repo_state(tmp_path)
    (worktree_path / "app.txt").write_text("worktree change\n", encoding="utf-8")

    exit_code = _cmd_dirty_worktree_gate(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "dirty_worktree_gate: open" in output
    assert "location_kind: task-worktree" in output
    assert "ownership: task-owned-worktree" in output
    assert f"task_id: {task.id}" in output
    assert "worktree_path: ../owned-worktree" in output
    assert "dirty_paths: app.txt" in output

def test_dirty_worktree_gate_reports_ambiguous_main_checkout_ownership(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    first = create_task(tmp_path, title="First interrupted task")
    second = create_task(tmp_path, title="Second interrupted task")
    for task in (first, second):
        task.status = "interrupted"
        task.pipeline_status = "testing"
        save_task(tmp_path, task)
        reports_dir = task_dir(tmp_path, task) / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "testing-001.yaml").write_text(
            yaml.safe_dump({"files_changed": ["app.txt"]}, sort_keys=False),
            encoding="utf-8",
        )
    _commit_repo_state(tmp_path)
    (tmp_path / "app.txt").write_text("changed\n", encoding="utf-8")

    exit_code = _cmd_dirty_worktree_gate(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "dirty_worktree_gate: blocked" in output
    assert "location_kind: main-checkout" in output
    assert "ownership: ambiguous-ownership" in output
    assert f"task_id: {first.id},{second.id}" in output

def test_queue_command_lists_parked_task_as_resumable_with_distinct_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    parked = create_task(tmp_path, title="Parked task", auto_commit=False)
    parked.status = "parked"
    parked.pipeline_status = "testing"
    parked.runtime.execution_status = "interrupted"
    parked.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="interrupted",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict=None,
        summary="Execution interrupted via `litehive stop`. Resume from `testing`.",
    )
    parked.runtime.interruption = RuntimeInterruptionState(
        source="runner",
        stage="testing",
        pipeline_status="testing",
        resume_stage="testing",
        reason="Task stopped via CLI",
        summary="Execution interrupted via `litehive stop`. Resume from `testing`.",
    )
    save_task(tmp_path, parked)
    save_task_runtime(tmp_path, parked)

    exit_code = _cmd_queue(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "queue_length: 1" in output
    assert "resumable_tasks: 1" in output
    assert (
        f"resume 1. {parked.id} [parked/testing] priority=medium engine=codex (default) model=default "
        "title=Parked task depends_on=-"
    ) in output

def test_dirty_worktree_gate_reports_missing_recorded_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    task = create_task(tmp_path, title="Missing worktree task")
    task.git.worktree_path = "../missing-worktree"
    save_task(tmp_path, task)
    _commit_repo_state(tmp_path)

    exit_code = _cmd_dirty_worktree_gate(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "dirty_worktree_gate: blocked" in output
    assert "location_kind: task-worktree" in output
    assert "ownership: missing-recorded-worktree" in output
    assert f"task_id: {task.id}" in output
    assert "worktree_path: ../missing-worktree" in output

def test_save_task_migrates_legacy_worktree_path_into_runtime_state(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Migrate legacy worktree mapping")

    task.git.worktree_path = ".litehive/worktrees/legacy-task"
    save_task(tmp_path, task)

    task_payload = yaml.safe_load(task_file(tmp_path, task).read_text(encoding="utf-8")) or {}
    runtime_payload = yaml.safe_load(task_runtime_file(tmp_path, task).read_text(encoding="utf-8")) or {}
    refreshed = require_task(tmp_path, task.id)

    assert task_payload["git"]["worktree_path"] is None
    assert runtime_payload["git"]["worktree_path"] == ".litehive/worktrees/legacy-task"
    assert refreshed.git.worktree_path is None
    assert refreshed.runtime.git.worktree_path == ".litehive/worktrees/legacy-task"
    assert get_task_worktree_path(refreshed) == ".litehive/worktrees/legacy-task"

def test_pool_summary_reports_closed_tasks_with_reason_and_follow_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        return SubagentResult(
            ref=SubagentRef(id="SA-stub", role=role, engine=engine_name, status="completed", path="subagents/stub"),
            execution=CLIExecutionResult(adapter=engine_name, argv=(engine_name, "exec"), cwd=tmp_path, exit_code=0,
                stdout="VERDICT: PASS\nSUMMARY: ok\nFILES_CHANGED:\n- app.txt\nTESTS_ADDED: 1\nTESTS_PASSING: 1\nWARNINGS:\n", stderr=""),
            transcript="", exit_code=0,
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    closed = create_task(tmp_path, title="Not now")
    follow_up = create_task(tmp_path, title="Revisit later")
    state = load_state(tmp_path)
    state.queue = [task_id for task_id in state.queue if task_id == closed.id]
    save_state(tmp_path, state)

    close_task(
        tmp_path,
        closed.id,
        outcome="deferred",
        reason="Revisit after launch",
        follow_up_task_id=follow_up.id,
    )

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            engine=None,
            model=None,
            drain=False,
            dry_run=False,
            stop_on_failure=None,
            max_tasks=None,
            stop_on_limit=None,
            quota_threshold=None,
            budget_threshold=None,
            pool_usage_cap=None,
            pool_cost_cap=None,
            engine_usage_cap=None,
            engine_budget_cap=None,
            engine_cost=None,
            stop_on_dirty_git=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "closed_tasks: 1" in output
    assert (
        f"closed: {closed.id} Not now status=deferred pipeline_status=backlog "
        f"stage_outcomes=- reason_code=deferred reason=Revisit after launch "
        f"follow_up_task={follow_up.id}"
    ) in output

def test_update_command_updates_task_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="opencode",
            model="zai-coding-plan/glm-5.1",
            retry_limit="2",
            priority="high",
            goal="Ship queue CLI",
            acceptance_criteria=["Task is visible in queue"],
            human_checkpoint=["before_acceptance"],
            task_type="research",
            mode="tasks",
            auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "opencode"
    assert updated.model == "zai-coding-plan/glm-5.1"
    assert updated.retry_policy.max_retries == 2
    assert updated.priority == "high"
    assert updated.goal == "Ship queue CLI"
    assert updated.acceptance_criteria == ["Task is visible in queue"]
    assert updated.human_checkpoints == ["before_acceptance"]
    assert updated.task_type == "research"
    assert updated.mode == "tasks"
    assert updated.git.auto_commit is False
    assert "engine: opencode" in output
    assert "model: zai-coding-plan/glm-5.1" in output
    assert "retry_limit: 2" in output
    assert "priority: high" in output
    assert "acceptance_criteria: 1" in output
    assert "human_checkpoints: before_acceptance" in output
    assert "task_type: research" in output

def test_update_command_can_clear_task_model_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task", model="gemini-2.5-pro")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            model="default",
            engine=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.model is None
    assert "model: default" in output

def test_update_command_seeds_template_defaults_when_switching_to_tasks_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Review queue behavior")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type="review",
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode="tasks",
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.mode == "tasks"
    assert updated.task_type == "review"
    assert (
        updated.goal
        == "Review the target change critically and produce an actionable decision with supporting evidence."
    )
    assert updated.acceptance_criteria
    assert updated.constraints
    assert updated.plan
    assert "acceptance_criteria: 3" in output

def test_update_command_can_clear_task_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task", task_type="review")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type="default",
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.task_type is None
    assert "task_type: -" in output

def test_update_command_clears_task_retry_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune retry policy", retry_limit=2)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine=None,
            retry_limit="default",
            acceptance_criteria=None,
            human_checkpoint=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "retry_limit: default" in output
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.retry_policy.max_retries is None

def test_update_command_accepts_gemini_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune Gemini task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="gemini",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "gemini"
    assert "engine: gemini" in output

def test_update_command_accepts_copilot_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune Copilot task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="copilot",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "copilot"
    assert "engine: copilot" in output

def test_daemon_foreground_stops_before_run_when_pre_status_has_explicit_pool_stop_reason(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".litehive").mkdir()
    (workspace / ".litehive" / "state.yaml").write_text(
        "active_task_id: T-0001\nqueue:\n  - T-0001\npool_stop_reason: max_tasks_reached\n",
        encoding="utf-8",
    )
    counts_dir = tmp_path / "counts"
    counts_dir.mkdir()
    run_count_file = counts_dir / "run-count"

    fake_uv = _write_fake_uv(
        tmp_path,
        f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "python" && "${{3:-}}" == "-" ]]; then
  shift 2
  exec python3 "$@"
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "run" ]]; then
  count="$(cat "{run_count_file}" 2>/dev/null || echo 0)"
  count="$((count + 1))"
  echo "$count" > "{run_count_file}"
  echo "unexpected run"
  exit 0
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "repair" ]]; then
  echo "repaired: no"
  exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )

    config_home = tmp_path / "config-home"
    result = subprocess.run(
        [sys.executable, "-m", "litehive.main", "daemon", "run", "--foreground", "--workspace", str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=_with_fake_uv(fake_uv, xdg_config_home=config_home),
        check=False,
    )

    assert result.returncode == 0
    assert "Pool already stopped: max_tasks_reached" in result.stdout
    assert not run_count_file.exists()

    log_dirs = list((workspace / ".litehive" / "logs" / "run-all").iterdir())
    assert len(log_dirs) == 1
    assert (log_dirs[0] / "0001-pre-status.log").exists()
    assert not (log_dirs[0] / "0001-run.log").exists()
    assert (config_home / "litehive" / "daemons.yaml").exists()

def test_daemon_foreground_restarts_litehive_until_queue_is_empty(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".litehive").mkdir()
    (workspace / ".litehive" / "state.yaml").write_text(
        "active_task_id: null\nqueue:\n  - T-0001\npool_stop_reason: null\n",
        encoding="utf-8",
    )
    counts_dir = tmp_path / "counts"
    counts_dir.mkdir()
    run_count_file = counts_dir / "run-count"

    fake_uv = _write_fake_uv(
        tmp_path,
        f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "python" && "${{3:-}}" == "-" ]]; then
  shift 2
  exec python3 "$@"
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "run" ]]; then
  count="$(cat "{run_count_file}" 2>/dev/null || echo 0)"
  count="$((count + 1))"
  echo "$count" > "{run_count_file}"
  if [[ "$count" -eq 1 ]]; then
    cat > "{workspace / ".litehive" / "state.yaml"}" <<'STATE'
active_task_id: null
queue:
  - T-0001
pool_stop_reason: null
STATE
  else
    cat > "{workspace / ".litehive" / "state.yaml"}" <<'STATE'
active_task_id: null
queue: []
pool_stop_reason: queue_exhausted
STATE
  fi
  echo "tasks_run: 1"
  echo "stop_reason: queue_exhausted"
  exit 0
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "repair" ]]; then
  echo "repaired: no"
  exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )

    result = subprocess.run(
        [sys.executable, "-m", "litehive.main", "daemon", "run", "--foreground", "--workspace", str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=_with_fake_uv(fake_uv, xdg_config_home=tmp_path / "config-home"),
        check=False,
    )

    assert result.returncode == 0
    assert "== iteration 1 ==" in result.stdout
    assert "== iteration 2 ==" in result.stdout
    assert "No active or queued tasks remain. Stopping." in result.stdout
    assert run_count_file.read_text(encoding="utf-8").strip() == "2"

    log_dirs = list((workspace / ".litehive" / "logs" / "run-all").iterdir())
    assert len(log_dirs) == 1
    log_dir = log_dirs[0]
    assert (log_dir / "0001-pre-status.log").exists()
    assert (log_dir / "0001-run.log").exists()
    assert (log_dir / "0001-post-status.log").exists()
    assert (log_dir / "0002-pre-status.log").exists()
    assert (log_dir / "0002-run.log").exists()
    assert (log_dir / "0002-post-status.log").exists()

def test_daemon_foreground_continues_after_task_requeued(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".litehive").mkdir()
    (workspace / ".litehive" / "state.yaml").write_text(
        "active_task_id: null\nqueue:\n  - T-0001\npool_stop_reason: null\n",
        encoding="utf-8",
    )
    counts_dir = tmp_path / "counts"
    counts_dir.mkdir()
    run_count_file = counts_dir / "run-count"

    fake_uv = _write_fake_uv(
        tmp_path,
        f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "python" && "${{3:-}}" == "-" ]]; then
  shift 2
  exec python3 "$@"
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "run" ]]; then
  count="$(cat "{run_count_file}" 2>/dev/null || echo 0)"
  count="$((count + 1))"
  echo "$count" > "{run_count_file}"
  if [[ "$count" -eq 1 ]]; then
    cat > "{workspace / ".litehive" / "state.yaml"}" <<'STATE'
active_task_id: null
queue:
  - T-0001
pool_stop_reason: task_requeued
STATE
    echo "tasks_run: 1"
    echo "stop_reason: task_requeued"
    exit 0
  fi

  cat > "{workspace / ".litehive" / "state.yaml"}" <<'STATE'
active_task_id: null
queue: []
pool_stop_reason: queue_exhausted
STATE
  echo "tasks_run: 1"
  echo "stop_reason: queue_exhausted"
  exit 0
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "repair" ]]; then
  echo "repaired: no"
  exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )

    result = subprocess.run(
        [sys.executable, "-m", "litehive.main", "daemon", "run", "--foreground", "--workspace", str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=_with_fake_uv(fake_uv, xdg_config_home=tmp_path / "config-home"),
        check=False,
    )

    assert result.returncode == 0
    assert "== iteration 1 ==" in result.stdout
    assert "== iteration 2 ==" in result.stdout
    assert "Stopping after litehive reported stop_reason: task_requeued" not in result.stdout
    assert "No active or queued tasks remain. Stopping." in result.stdout
    assert run_count_file.read_text(encoding="utf-8").strip() == "2"

    log_dirs = list((workspace / ".litehive" / "logs" / "run-all").iterdir())
    assert len(log_dirs) == 1
    log_dir = log_dirs[0]
    assert (log_dir / "0001-run.log").exists()
    assert (log_dir / "0001-post-status.log").exists()
    assert (log_dir / "0002-run.log").exists()
    assert (log_dir / "0002-post-status.log").exists()

def test_daemon_background_lifecycle_and_global_instances_registry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".litehive").mkdir()
    (workspace / ".litehive" / "state.yaml").write_text(
        "active_task_id: null\nqueue:\n  - T-0001\npool_stop_reason: null\n",
        encoding="utf-8",
    )
    counts_dir = tmp_path / "counts"
    counts_dir.mkdir()
    keepalive = counts_dir / "keepalive"
    keepalive.write_text("1\n", encoding="utf-8")
    run_started = counts_dir / "run-started"

    fake_uv = _write_fake_uv(
        tmp_path,
        f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "python" && "${{3:-}}" == "-" ]]; then
  shift 2
  exec python3 "$@"
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "run" ]]; then
  trap 'exit 0' TERM INT
  echo started > "{run_started}"
  while [[ -f "{keepalive}" ]]; do
    sleep 0.1
  done
  cat > "{workspace / ".litehive" / "state.yaml"}" <<'STATE'
active_task_id: null
queue: []
pool_stop_reason: queue_exhausted
STATE
  echo "tasks_run: 1"
  echo "stop_reason: queue_exhausted"
  exit 0
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "repair" ]]; then
  echo "repaired: no"
  exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )

    config_home = tmp_path / "config-home"
    registry = config_home / "litehive" / "daemons.yaml"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        yaml.safe_dump(
            {
                "daemons": {
                    "/stale/workspace": {
                        "workspace": "/stale/workspace",
                        "pid": 999999,
                        "started_at": "2026-04-04T10:00:00+00:00",
                        "log_dir": "/stale/logs",
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    env = _with_fake_uv(fake_uv, xdg_config_home=config_home)

    start = subprocess.run(
        [sys.executable, "-m", "litehive.main", "daemon", "run", "--workspace", str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert start.returncode == 0
    first_pid = int(next(line.split(": ", 1)[1] for line in start.stdout.splitlines() if line.startswith("pid: ")))

    deadline = time.time() + 5
    while time.time() < deadline and not run_started.exists():
        time.sleep(0.1)
    assert run_started.exists()

    second_start = subprocess.run(
        [sys.executable, "-m", "litehive.main", "daemon", "run", "--workspace", str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert second_start.returncode == 1
    assert "daemon already running" in second_start.stdout

    status = subprocess.run(
        [sys.executable, "-m", "litehive.main", "daemon", "status", "--workspace", str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert status.returncode == 0
    assert "daemon_status: running" in status.stdout
    assert f"pid: {first_pid}" in status.stdout

    instances = subprocess.run(
        [sys.executable, "-m", "litehive.main", "daemon", "instances"],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert instances.returncode == 0
    assert "instances: 1" in instances.stdout
    assert str(workspace.resolve()) in instances.stdout
    assert "/stale/workspace" not in instances.stdout
    registry_data = yaml.safe_load(registry.read_text(encoding="utf-8"))
    assert "/stale/workspace" not in registry_data["daemons"]

    restart = subprocess.run(
        [sys.executable, "-m", "litehive.main", "daemon", "restart", "--workspace", str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert restart.returncode == 0
    second_pid = int(next(line.split(": ", 1)[1] for line in restart.stdout.splitlines() if line.startswith("pid: ")))
    assert second_pid != first_pid
    assert f"previous_pid: {first_pid}" in restart.stdout

    stop = subprocess.run(
        [sys.executable, "-m", "litehive.main", "daemon", "stop", "--workspace", str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert stop.returncode == 0
    assert "daemon_status: stopped" in stop.stdout
    registry_data = yaml.safe_load(registry.read_text(encoding="utf-8"))
    assert registry_data["daemons"] == {}


def test_run_daemon_loop_prunes_old_run_all_log_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import litehive.daemon as daemon_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".litehive").mkdir()
    (workspace / ".litehive" / "state.yaml").write_text(
        "active_task_id: null\nqueue: []\npool_stop_reason: null\n",
        encoding="utf-8",
    )
    logs_root = workspace / ".litehive" / "logs" / "run-all"
    logs_root.mkdir(parents=True, exist_ok=True)
    for index in range(10):
        directory = logs_root / f"20260404T10000{index}Z"
        directory.mkdir()
        (directory / "0001-run.log").write_text(f"old {index}\n", encoding="utf-8")

    fake_uv = _write_fake_uv(
        tmp_path,
        """#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "run" && "${2:-}" == "litehive" && "${3:-}" == "repair" ]]; then
  echo "repaired: no"
  exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )
    monkeypatch.setattr(
        "litehive.daemon._default_command_prefix",
        lambda: [str(fake_uv), "run", "litehive"],
    )

    exit_code = daemon_module.run_daemon_loop(workspace, output_stream=None)

    assert exit_code == 0
    directories = sorted(path.name for path in logs_root.iterdir() if path.is_dir())
    assert len(directories) == 8
    assert "20260404T100000Z" not in directories
    assert "20260404T100001Z" not in directories
    assert any(name.startswith("2026") for name in directories)

def test_run_task_skips_pre_acceptance_hook_when_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="No hook configured", auto_commit=False)
    real_run = subprocess.run

    def fail_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        argv = args[0] if args else kwargs.get("args")
        if list(argv) == ["bash", "-lc", "uv run ruff check litehive tests"]:
            raise AssertionError("pre-acceptance command should not run")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(  # type: ignore[no-untyped-def]
                tmp_path, task.pipeline_status, task=task
            )
        ),
    )
    monkeypatch.setattr("litehive.runtime.subprocess.run", fail_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"

def test_stage_report_from_subagent_structures_resource_limit_failures(tmp_path: Path) -> None:
    task = TaskRecord(id="T-0001", slug="native-task", title="Native task")

    report = stage_report_from_subagent(
        task,
        "implementing",
        _resource_limited_subagent_result(tmp_path, "implementing"),
    )

    assert report.verdict == "blocked"
    assert report.summary == "implementing blocked: memory limit exceeded (OOM)"
    assert report.resource_limit_event is not None
    assert report.resource_limit_event.resource == "memory"

def test_run_next_task_records_structured_resource_limit_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Native code task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _resource_limited_subagent_result(  # type: ignore[no-untyped-def]
                tmp_path, "grooming", engine_name=engine_name
            )
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_outcome.reason_code == "resource_limit"
    assert task.runtime.last_outcome.reason == "grooming blocked: memory limit exceeded (OOM)"

    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-native-code-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["outcome_reason_code"] == "resource_limit"
    assert report["resource_limit_event"]["resource"] == "memory"

def test_render_task_summary_includes_resource_limit_signal_and_effective_limits() -> None:
    task = TaskRecord(id="T-0001", slug="native-task", title="Native task")
    task.runtime.last_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="failed",
        path="subagents/SA-0001-swe",
        sandboxed=True,
        sandbox_summary="sandbox[docker:litehive-external-engine:latest net=none workspace=rw limits=memory=4096m,cpus=2,pids=256]",
        started_at="2026-04-01T10:00:00+00:00",
        updated_at="2026-04-01T10:01:00+00:00",
        completed_at="2026-04-01T10:01:00+00:00",
        exit_code=137,
        transcript_snippet="OOMKilled",
        resource_limit_event=ResourceLimitEvent(
            resource="memory",
            reason="memory limit exceeded (OOM)",
            observed_signal="oom",
            exit_code=137,
            memory_mb=4096,
            cpu_count=2.0,
            process_limit=256,
        ),
    )

    lines = render_task_summary(task, active=False)

    assert any(
        "resource_limit=memory signal=oom exit_code=137 limits=memory_mb=4096,cpu_count=2,process_limit=256"
        in line
        for line in lines
    )

def test_run_task_runs_pre_acceptance_hook_after_testing_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(pre_acceptance_command="uv run ruff check litehive tests"),
    )
    create_task(tmp_path, title="Run ruff before acceptance", auto_commit=False)
    calls: list[str] = []
    real_run = subprocess.run

    def fake_subagent_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        calls.append(task.pipeline_status)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    def fake_hook(argv, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        if list(argv) != ["bash", "-lc", "uv run ruff check litehive tests"]:
            return real_run(argv, cwd=cwd, capture_output=capture_output, text=text, check=check)
        assert list(argv) == ["bash", "-lc", "uv run ruff check litehive tests"]
        assert cwd == tmp_path
        assert capture_output is True
        assert text is True
        assert check is False
        return subprocess.CompletedProcess(argv, 0, stdout="ruff clean\n", stderr="")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_subagent_run)
    monkeypatch.setattr("litehive.runtime.subprocess.run", fake_hook)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert calls == ["grooming", "implementing", "testing", "accepting"]
    artifact = (
        tmp_path
        / ".litehive"
        / "tasks"
        / "T-0001-run-ruff-before-acceptance"
        / "artifacts"
        / "pre-acceptance-hook.txt"
    )
    assert "command: uv run ruff check litehive tests" in artifact.read_text(encoding="utf-8")
    accepting_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-run-ruff-before-acceptance"
            / "reports"
            / "accepting-004.yaml"
        ).read_text(encoding="utf-8")
    )
    assert accepting_report["hook_results"][0]["point"] == "before_pm_acceptance"
    assert "runner hook passed" in "\n".join(accepting_report["warnings"])

def test_run_task_blocks_before_accepting_when_pre_acceptance_hook_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(pre_acceptance_command="uv run ruff check litehive tests"),
    )
    create_task(tmp_path, title="Block on failing ruff", auto_commit=False)
    calls: list[str] = []

    def fake_subagent_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if role == "recovery":
            return _failed_subagent_result(tmp_path, task.pipeline_status, task=task)
        calls.append(task.pipeline_status)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    def fake_hook(argv, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="F401 unused import\n")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_subagent_run)
    monkeypatch.setattr("litehive.runtime.subprocess.run", fake_hook)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    assert calls == ["grooming", "implementing", "testing"]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "flagged"
    assert task.pipeline_status == "accepting"
    assert task.runtime.last_outcome.kind == "blocked"
    accepting_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-block-on-failing-ruff"
            / "reports"
            / "accepting-004.yaml"
        ).read_text(encoding="utf-8")
    )
    assert accepting_report["verdict"] == "blocked"
    assert "accepting blocked by runner hook" in accepting_report["summary"]
    assert accepting_report["hook_results"][0]["point"] == "before_pm_acceptance"
    assert "runner hook failed" in "\n".join(accepting_report["warnings"])

def test_run_task_records_non_blocking_runner_hook_failure_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={
                "before_swe_implementation": [{"command": "echo pre && exit 7", "blocking": False}]
            }
        ),
    )
    create_task(tmp_path, title="Warn on hook failure", auto_commit=False)
    calls: list[str] = []

    def fake_subagent_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        calls.append(task.pipeline_status)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    def fake_hook(argv, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        if list(argv) == ["bash", "-lc", "echo pre && exit 7"]:
            return subprocess.CompletedProcess(argv, 7, stdout="pre\n", stderr="hook warning\n")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_subagent_run)
    monkeypatch.setattr("litehive.runtime.subprocess.run", fake_hook)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert calls == ["grooming", "implementing", "testing", "accepting"]
    implementing_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-warn-on-hook-failure"
            / "reports"
            / "implementing-002.yaml"
        ).read_text(encoding="utf-8")
    )
    assert implementing_report["hook_results"][0]["point"] == "before_swe_implementation"
    assert implementing_report["hook_results"][0]["status"] == "failed"
    assert implementing_report["verdict"] == "pass"
    assert "runner hook failed" in "\n".join(implementing_report["warnings"])

def test_run_task_blocks_when_post_implementation_runner_hook_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={
                "after_swe_implementation": [{"command": "echo post && exit 9", "blocking": True}]
            }
        ),
    )
    create_task(tmp_path, title="Block on post-implementation hook", auto_commit=False)
    calls: list[str] = []

    def fake_subagent_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        if role == "recovery":
            return _failed_subagent_result(tmp_path, task.pipeline_status, task=task)
        calls.append(task.pipeline_status)
        # Don't write CLI verdict for implementing — the blocking post-hook
        # overrides the verdict to "blocked".  Writing a "pass" CLI verdict
        # would leak into the recovery thread scan and mask the block.
        if task.pipeline_status == "implementing":
            return _completed_subagent_result(tmp_path, task.pipeline_status)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    def fake_hook(argv, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        if list(argv) == ["bash", "-lc", "echo post && exit 9"]:
            return subprocess.CompletedProcess(argv, 9, stdout="post\n", stderr="bad diff\n")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_subagent_run)
    monkeypatch.setattr("litehive.runtime.subprocess.run", fake_hook)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    assert calls == ["grooming", "implementing"]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "flagged"
    assert task.pipeline_status == "implementing"
    implementing_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-block-on-post-implementation-hook"
            / "reports"
            / "implementing-002.yaml"
        ).read_text(encoding="utf-8")
    )
    assert implementing_report["verdict"] == "blocked"
    assert implementing_report["hook_results"][0]["point"] == "after_swe_implementation"

def test_run_task_runs_after_acceptance_runner_hook_on_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={"after_pm_acceptance": [{"command": "echo accepted", "blocking": True}]}
        ),
    )
    create_task(tmp_path, title="Run after acceptance hook", auto_commit=False)

    def fake_subagent_run(self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    def fake_hook(argv, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        if list(argv) == ["bash", "-lc", "echo accepted"]:
            return subprocess.CompletedProcess(argv, 0, stdout="accepted\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_subagent_run)
    monkeypatch.setattr("litehive.runtime.subprocess.run", fake_hook)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    accepting_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-run-after-acceptance-hook"
            / "reports"
            / "accepting-004.yaml"
        ).read_text(encoding="utf-8")
    )
    assert accepting_report["hook_results"][0]["point"] == "after_pm_acceptance"


def test_add_command_with_priority_high(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="High priority task",
            goal="",
            pm_complexity=None,
            planned_effort=None,
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            engine=None,
            model=None,
            retry_limit=None,
            no_auto_commit=False,
            priority="high",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.priority == "high"
    assert "priority: high" in output


def test_add_command_default_priority_is_medium(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Default priority task",
            goal="",
            pm_complexity=None,
            planned_effort=None,
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            engine=None,
            model=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.priority == "medium"
    assert "priority: medium" in output


def test_add_command_with_priority_critical(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Critical priority task",
            goal="",
            pm_complexity=None,
            planned_effort=None,
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            engine=None,
            model=None,
            retry_limit=None,
            no_auto_commit=False,
            priority="critical",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.priority == "critical"
    assert "priority: critical" in output
