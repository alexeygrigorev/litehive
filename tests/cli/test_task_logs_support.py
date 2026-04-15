from pathlib import Path

from litehive.cli.task_logs_support import _load_task_with_runtime
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task
from litehive.tasks.paths import task_dir


def test_load_task_with_runtime_tolerates_unrelated_missing_runtime_rows(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Journal task", auto_commit=False)
    journal = task_dir(tmp_path, task) / "journal.md"
    journal.write_text("# journal\nentry\n", encoding="utf-8")

    missing_dir = tmp_path / ".litehive" / "tasks" / "T-0002-missing-runtime"
    missing_dir.mkdir(parents=True)
    (missing_dir / "task.yaml").write_text(
        "id: T-0002\nslug: missing-runtime\ntitle: Missing runtime row\npipeline_mode: full\npriority: medium\ngit:\n  auto_commit: true\n  commit_message: missing runtime row\n",
        encoding="utf-8",
    )

    loaded = _load_task_with_runtime(tmp_path, task.id)

    assert loaded is not None
    assert loaded.id == task.id
