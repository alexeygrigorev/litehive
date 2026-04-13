from pathlib import Path
import importlib

import yaml
from typer.testing import CliRunner

import litehive.cli as legacy_cli
from litehive.state.records import load_task_record_file, serialize_task_record
from tests.workspace_helpers import ensure_workspace

modern_cli = importlib.import_module("litehive.cli.app")


def _typer_app(candidate):
    return candidate.app if hasattr(candidate, "app") else candidate


def test_load_task_record_ignores_legacy_top_level_engine(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task_file = tmp_path / ".litehive" / "tasks" / "T-0001-legacy" / "task.yaml"
    task_file.parent.mkdir(parents=True)
    task_file.write_text(
        yaml.safe_dump(
            {
                "id": "T-0001",
                "slug": "legacy",
                "title": "Legacy task",
                "engine": "codex",
                "subagents": [
                    {
                        "id": "SA-0001",
                        "role": "swe",
                        "engine": "gemini",
                        "status": "completed",
                        "path": "subagents/SA-0001-swe",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    task = load_task_record_file(task_file)
    serialized = serialize_task_record(task)

    assert task.id == "T-0001"
    assert "engine: codex" not in serialized
    assert "subagents:" not in serialized


def test_task_add_help_omits_engine_flag() -> None:
    for app in (_typer_app(legacy_cli.app), _typer_app(modern_cli.app)):
        result = CliRunner().invoke(app, ["task", "add", "--help"])
        assert result.exit_code == 0, result.output
        assert "--engine" not in result.output


def test_task_update_help_omits_engine_flag() -> None:
    for app in (_typer_app(legacy_cli.app), _typer_app(modern_cli.app)):
        result = CliRunner().invoke(app, ["task", "update", "--help"])
        assert result.exit_code == 0, result.output
        assert "--engine" not in result.output
