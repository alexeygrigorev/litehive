"""Helpers for child-process environments that target a workspace or checkout."""

from collections.abc import Iterable, Mapping
import os
from pathlib import Path


def _resolve_project_root(path: Path) -> Path:
    candidate = path.resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for current in (candidate, *candidate.parents):
        if (current / "pyproject.toml").exists():
            return current
        if (current / ".git").exists():
            return current
    return candidate


def build_child_process_env(
    *,
    target_root: Path,
    extra_env: Mapping[str, str] | None = None,
    base_env: Mapping[str, str] | None = None,
    stripped_env_vars: Iterable[str] = (),
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    caller_project_root = _resolve_project_root(Path.cwd())
    target_project_root = _resolve_project_root(target_root)
    if target_project_root != caller_project_root:
        env.pop("VIRTUAL_ENV", None)
    for key in stripped_env_vars:
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    return env


__all__ = ["build_child_process_env"]
