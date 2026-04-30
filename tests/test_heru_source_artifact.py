from pathlib import Path
import tomllib


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _heru_requirement(pyproject_data: dict) -> str:
    dependencies = pyproject_data.get("project", {}).get("dependencies", [])
    for dependency in dependencies:
        if isinstance(dependency, str) and dependency.startswith("heru>="):
            return dependency.removeprefix("heru>=")
    raise AssertionError("pyproject.toml must declare heru>=...")


def test_repo_pins_local_heru_checkout() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pyproject_data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    uv_config = (pyproject_data.get("tool") or {}).get("uv") or {}
    heru_source = (uv_config.get("sources") or {}).get("heru")

    assert isinstance(heru_source, dict)
    configured_path = heru_source.get("path")
    assert isinstance(configured_path, str) and configured_path

    assert heru_source.get("editable") is True
    assert "pip" not in uv_config
    heru_path = (repo_root / configured_path).resolve()
    assert heru_path.exists()
    assert heru_path.is_dir()

    minimum_version = _heru_requirement(pyproject_data)
    heru_pyproject = tomllib.loads((heru_path / "pyproject.toml").read_text(encoding="utf-8"))
    heru_version = heru_pyproject["project"]["version"]
    assert _version_tuple(heru_version) >= _version_tuple(minimum_version)

    lock_data = tomllib.loads((repo_root / "uv.lock").read_text(encoding="utf-8"))
    heru_lock = next(package for package in lock_data["package"] if package["name"] == "heru")
    assert heru_lock["version"] == heru_version
    assert heru_lock["source"] == {"editable": configured_path}


def test_repo_does_not_vendor_heru_wheels() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert not list((repo_root / "packages").glob("heru-*.whl"))
