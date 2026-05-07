import subprocess
from pathlib import Path

from litehive.git.ops import check_origin_divergence


def test_check_origin_divergence_compares_main_even_when_head_is_elsewhere(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    workspace = tmp_path / "workspace"
    remote_clone = tmp_path / "remote-clone"

    subprocess.run(["git", "init", "--bare", str(origin)], check=True)
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=workspace, check=True)

    readme = workspace / "README.md"
    readme.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=workspace, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=workspace, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=workspace, check=True)

    readme.write_text("local\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "local"], cwd=workspace, check=True)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=workspace, check=True)

    subprocess.run(["git", "clone", str(origin), str(remote_clone)], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=remote_clone, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=remote_clone, check=True)
    remote_readme = remote_clone / "README.md"
    remote_readme.write_text("remote\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "remote"], cwd=remote_clone, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=remote_clone, check=True)

    message = check_origin_divergence(workspace)

    assert message is not None
    assert "local main (" in message
    assert "origin/main (" in message
    assert "Manual reconciliation required" in message
