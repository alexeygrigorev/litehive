from pathlib import Path
import json

from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.workspace import ensure_workspace
from litehive.state.records import list_tasks


def test_import_subapp_help_lists_spec_and_github() -> None:
    result = CliRunner().invoke(app, ["import", "--help"], standalone_mode=False)

    assert result.exit_code == 0, result.output
    assert "spec" in result.output
    assert "github" in result.output


def test_import_spec_creates_task_from_file(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    spec_path = tmp_path / "feature.md"
    spec_path.write_text("# Add CSV export\n\nUsers can export reports as CSV.\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["import", "spec", str(spec_path), "--workspace", str(tmp_path), "--task-type", "docs"],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert "Created task T-0001: Add CSV export" in result.output
    task = list_tasks(tmp_path)[0]
    assert task.title == "Add CSV export"
    assert task.task_type == "docs"
    assert task.goal == "# Add CSV export\n\nUsers can export reports as CSV."
    brief_path = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "brief.md"
    assert brief_path.read_text(encoding="utf-8") == "# Add CSV export\n\nUsers can export reports as CSV.\n"


def test_import_github_creates_task_from_issue(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    issue_payload = {
        "number": 10,
        "title": "Fix login redirect",
        "body": "The login flow loops back to /login.",
        "labels": [{"name": "bug"}, {"name": "priority: high"}],
        "url": "https://github.com/owner/repo/issues/10",
    }

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if cmd[:3] == ["gh", "auth", "status"]:
            return type("Proc", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
        if cmd[:4] == ["gh", "issue", "view", "10"]:
            return type(
                "Proc",
                (),
                {"returncode": 0, "stdout": json.dumps(issue_payload), "stderr": ""},
            )()
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("litehive.cli.import_cli.subprocess.run", fake_run)

    result = CliRunner().invoke(
        app,
        ["import", "github", "10", "--repo", "owner/repo", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )

    assert result.exit_code == 0, result.output
    assert "Created task T-0001 from owner/repo#10" in result.output
    task = list_tasks(tmp_path)[0]
    assert task.title == "Fix login redirect"
    assert task.task_type == "bugfix"
    assert task.priority == "high"
    assert "Imported from GitHub issue: owner/repo#10" in task.goal


def test_import_github_help_exists() -> None:
    result = CliRunner().invoke(app, ["import", "github", "--help"], standalone_mode=False)

    assert result.exit_code == 0, result.output
    assert "Import GitHub issues as Litehive tasks" in result.output
