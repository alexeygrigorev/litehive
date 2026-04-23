from pathlib import Path
import subprocess
import tomllib


def test_repo_pins_checked_in_heru_artifact() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pyproject_data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    heru_source = (((pyproject_data.get("tool") or {}).get("uv") or {}).get("sources") or {}).get("heru")

    assert isinstance(heru_source, dict)
    configured_path = heru_source.get("path")
    assert isinstance(configured_path, str) and configured_path

    artifact_path = repo_root / configured_path
    assert artifact_path.exists()
    assert configured_path in (repo_root / "uv.lock").read_text(encoding="utf-8")

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", configured_path],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert tracked.returncode == 0, tracked.stderr.strip() or tracked.stdout.strip()

    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", configured_path],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 1, ignored.stdout.strip() or ignored.stderr.strip()
