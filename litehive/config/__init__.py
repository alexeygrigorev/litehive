"""Public facade for Litehive configuration helpers."""

from copy import deepcopy

from litehive.config.constants import *
from litehive.config.dataclasses import *
from litehive.config.formatting import (
    format_external_engine_sandbox as format_external_engine_sandbox,
    format_runner_hooks as format_runner_hooks,
    format_subagent_resource_limits as format_subagent_resource_limits,
)
from litehive.config.loading import (
    _merge_config_layers as _merge_config_layers,
    _read_config_mapping as _read_config_mapping,
    load_config as load_config,
    load_context as load_context,
    load_effective_config_data as load_effective_config_data,
)
from litehive.config.model import LitehiveConfig as LitehiveConfig
from litehive.config.normalization import *
from litehive.config.paths import (
    config_path as config_path,
    context_path as context_path,
    daemon_config_dir as daemon_config_dir,
    daemon_registry_path as daemon_registry_path,
    global_config_path as global_config_path,
    state_path as state_path,
    workspace_dir as workspace_dir,
    workspace_gitignore_path as workspace_gitignore_path,
)
from litehive.config.profiles import *
from litehive.config.retry import (
    _default_execution_retry_policies as _default_execution_retry_policies,
)
from litehive.config.workspace import (
    ensure_workspace as ensure_workspace,
    render_workspace_gitignore as render_workspace_gitignore,
)


DEFAULT_AGENT_STARTUP_GUIDANCE: dict[str, list[str]] = {
    "recovery": [
        "Your job is to diagnose why the previous agent failed and fix Litehive infrastructure bugs so the next stage retry can succeed.",
        "Do not redo the failed stage's work, do not implement the task itself, and do not submit the failed stage's verdict on the prior agent's behalf.",
        "Start with the failed subagent artifacts: stdout, stderr, transcript, session metadata, exit code, and any `litehive report` attempt or error.",
        "Submit your own recovery verdict describing the Litehive root cause you found, the fix you made, and why the failed stage should be retried.",
    ]
}


def default_agent_startup_guidance() -> dict[str, list[str]]:
    """Return built-in startup guidance that supplements workspace config."""
    return deepcopy(DEFAULT_AGENT_STARTUP_GUIDANCE)
