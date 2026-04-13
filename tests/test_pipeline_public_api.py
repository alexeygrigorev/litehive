from pathlib import Path

import litehive.pipeline as pipeline


def test_pipeline_package_exports_required_public_names():
    required = {
        "run_task",
        "TaskPoolStopConditions",
        "recover_completed_task",
        "rollback_completed_task",
        "inspect_dirty_worktree_gate",
        "active_engine_freezes",
        "is_engine_frozen",
        "repair_workspace_state",
    }

    assert required.issubset(set(pipeline.__all__))
    for name in required:
        assert hasattr(pipeline, name)


def test_pipeline_package_surface_stays_thin_and_public():
    init_path = Path(__file__).resolve().parents[1] / "litehive" / "pipeline" / "__init__.py"

    assert init_path.read_text(encoding="utf-8").count("\n") + 1 < 30
    assert all(not name.startswith("_") for name in pipeline.__all__)
