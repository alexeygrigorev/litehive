"""Process profile loader."""

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ._rendering import render_context_template as render_context_template

_PROFILE_DIR = Path(__file__).resolve().parent
_LIST_KEYS = {"development_rules", "tool_usage", "workspace_overlay", "specifics", "prompt_scaffold", "init_scaffold"}


@lru_cache(maxsize=1)
def _load_profiles() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    shared = yaml.safe_load((_PROFILE_DIR / "_shared.yaml").read_text(encoding="utf-8")) or {}
    overlays = {path.stem: yaml.safe_load(path.read_text(encoding="utf-8")) or {} for path in sorted(_PROFILE_DIR.glob("*.yaml")) if path.name != "_shared.yaml"}
    return shared, overlays


PROCESS_PROFILES = _load_profiles()[1]


def available_process_profiles() -> list[str]:
    return sorted(PROCESS_PROFILES)


def resolve_process_profile(name: str | None) -> dict[str, Any]:
    shared, profiles = _load_profiles()
    profile = deepcopy(shared)
    overlay = {} if name is None else profiles.get(name, profiles["generic"])
    for key, value in overlay.items():
        if key in _LIST_KEYS:
            profile[key].extend(deepcopy(value))
        elif key in {"stage_overlay", "stage_instructions"}:
            for stage, instructions in value.items():
                profile[key].setdefault(stage, []).extend(deepcopy(instructions))
        else:
            profile[key] = deepcopy(value)
    return profile
