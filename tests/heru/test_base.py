import os
from pathlib import Path
import sys

import heru.base as heru_base


def test_build_invocation_env_strips_stale_litehive_session_env_outside_workspace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    external_cwd = tmp_path / "external"
    workspace_root.mkdir()
    external_cwd.mkdir()

    monkeypatch.setenv("LITEHIVE_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("LITEHIVE_TASK_ID", "T-9999")
    monkeypatch.setenv("LITEHIVE_AGENT_ROLE", "swe")
    monkeypatch.setenv("LITEHIVE_STAGE", "implementing")

    env = heru_base.build_invocation_env(cwd=external_cwd)

    assert "LITEHIVE_WORKSPACE_ROOT" not in env
    assert "LITEHIVE_TASK_ID" not in env
    assert "LITEHIVE_AGENT_ROLE" not in env
    assert "LITEHIVE_STAGE" not in env


def test_build_invocation_env_prefers_current_python_bin_for_litehive(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    python_bin_dir = str(Path(sys.executable).parent)

    def fake_which(binary: str, path: str | None = None) -> str | None:
        if binary == "litehive" and path == python_bin_dir:
            return str(Path(python_bin_dir) / "litehive")
        return None

    monkeypatch.setattr(heru_base.shutil, "which", fake_which)

    env = heru_base.build_invocation_env(cwd=tmp_path)

    assert env["PATH"].split(os.pathsep)[0] == python_bin_dir
