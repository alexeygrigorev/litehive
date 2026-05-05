"""Process profile loader."""

from copy import deepcopy
from typing import Any

from litehive.config.profiles.defaults import PROCESS_PROFILE_OVERLAYS, SHARED_PROCESS_PROFILE
from litehive.config.profiles.model import ProcessProfile

_LIST_KEYS = {
    "development_rules",
    "tool_usage",
    "workspace_overlay",
    "specifics",
    "prompt_scaffold",
    "init_scaffold",
}


PROCESS_PROFILES = PROCESS_PROFILE_OVERLAYS


def available_process_profiles() -> list[str]:
    return sorted(PROCESS_PROFILES)


def resolve_process_profile(name: str | None) -> ProcessProfile:
    profile: dict[str, Any] = deepcopy(SHARED_PROCESS_PROFILE)
    if name is None:
        overlay: dict[str, Any] = {}
    else:
        if name not in PROCESS_PROFILES:
            available_profiles = ", ".join(sorted(PROCESS_PROFILES.keys()))
            raise ValueError(f"unknown process profile {name!r}; must be one of: {available_profiles}")
        overlay = PROCESS_PROFILES[name]
    for key, value in overlay.items():
        if key in _LIST_KEYS:
            profile[key].extend(deepcopy(value))
            continue
        if key in {"stage_overlay", "stage_instructions"}:
            for stage, instructions in value.items():
                profile[key].setdefault(stage, []).extend(deepcopy(instructions))
            continue
        profile[key] = deepcopy(value)
    return ProcessProfile.from_dict(profile)
