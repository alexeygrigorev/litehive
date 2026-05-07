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


def available_process_profiles() -> list[str]:
    """Return the sorted list of process-profile names for `litehive workspace init` selection and tab-completion."""
    return sorted(PROCESS_PROFILE_OVERLAYS)


def resolve_process_profile(name: str | None) -> ProcessProfile:
    """Materialize the requested process profile by overlaying its diff onto the shared base; raises ``ValueError`` for unknown names so config loading fails loudly instead of silently returning an empty profile."""
    profile: dict[str, Any] = deepcopy(SHARED_PROCESS_PROFILE)
    if name is None:
        overlay = PROCESS_PROFILE_OVERLAYS["generic"]
    elif name in PROCESS_PROFILE_OVERLAYS:
        overlay = PROCESS_PROFILE_OVERLAYS[name]
    else:
        available_profiles = ", ".join(available_process_profiles())
        raise ValueError(f"unknown process profile {name!r}; must be one of: {available_profiles}")
    for key, value in overlay.items():
        if key in _LIST_KEYS:
            profile[key].extend(deepcopy(value))
            continue
        if key in {"stage_overlay", "stage_instructions"}:
            for stage, instructions in value.items():
                profile[key].setdefault(stage, []).extend(deepcopy(instructions))
            continue
        profile[key] = deepcopy(value)
    return ProcessProfile.model_validate(profile)
