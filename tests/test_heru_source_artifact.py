from pathlib import Path
import tomllib


def test_repo_pins_local_heru_checkout() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pyproject_data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    heru_source = (((pyproject_data.get("tool") or {}).get("uv") or {}).get("sources") or {}).get("heru")

    assert isinstance(heru_source, dict)
    configured_path = heru_source.get("path")
    assert isinstance(configured_path, str) and configured_path

    assert heru_source.get("editable") is True
    heru_path = (repo_root / configured_path).resolve()
    assert heru_path.exists()
    assert heru_path.is_dir()
    assert configured_path in (repo_root / "uv.lock").read_text(encoding="utf-8")
