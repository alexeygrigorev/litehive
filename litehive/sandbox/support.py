"""Shared sandbox helper functions used by launcher policy code."""

from pathlib import Path
from typing import Mapping


def forced_engine_rw_state_dirs(
    engine_name: str,
    policy: object | None,
    env: Mapping[str, str] | None = None,
) -> frozenset[Path]:
    """
    Return the state dirs an engine must be able to write into.

    Operators sometimes classify these paths read-only in the
    workspace policy; the sandbox launcher consults this set so it
    can promote them to rw regardless and prevent silent breakage of
    engine session/rollout writes.
    """

    effective_env = dict(env or {})
    setenv = getattr(policy, "setenv", None)
    if isinstance(setenv, dict):
        effective_env.update(setenv)
    home_override = effective_env.get("HOME")
    if home_override:
        home = Path(home_override).expanduser()
    else:
        home = Path.home()

    candidates: list[Path] = []
    if engine_name == "codex":
        codex_home = effective_env.get("CODEX_HOME")
        if codex_home:
            codex_path = Path(codex_home).expanduser()
        else:
            codex_path = home / ".codex"
        candidates.append(codex_path)
    elif engine_name == "claude":
        candidates.append(home / ".claude")
    elif engine_name == "copilot":
        candidates.append(home / ".copilot")
    elif engine_name == "gemini":
        candidates.append(home / ".gemini")
    elif engine_name == "opencode":
        candidates.append(home / ".config" / "opencode")
    elif engine_name == "goz":
        candidates.append(home / ".goz")
        candidates.append(home / ".config" / "goz")

    resolved: set[Path] = set()
    for candidate in candidates:
        try:
            resolved.add(candidate.resolve())
        except OSError:
            continue
    return frozenset(resolved)


def sanitize_path_env(raw_path: str) -> str:
    """
    Drop PATH segments that point at ephemeral codex arg0 dirs.

    Codex spawns a wrapper process whose temp PATH segment vanishes
    when the run ends; carrying that segment into a child engine
    invocation produces a stale PATH entry that can break shelling
    out. Stripping it here keeps the propagated PATH usable across
    the sandbox boundary.
    """

    if not raw_path:
        return raw_path
    kept: list[str] = []
    for segment in raw_path.split(":"):
        if not segment:
            continue
        if "codex-arg0" in segment:
            continue
        if "codex-linux-" in segment and segment.endswith("/path"):
            continue
        kept.append(segment)
    return ":".join(kept)
