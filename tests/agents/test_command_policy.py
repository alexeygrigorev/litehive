from litehive.agents.command_policy import AGENT_BLOCKED_COMMAND_MESSAGE, agent_command_is_allowed


def test_pm_agents_can_shape_tasks() -> None:
    assert agent_command_is_allowed("planner", ["task", "add", "Follow-up"])
    assert agent_command_is_allowed("reviewer", ["task", "update", "T-0001"])
    assert agent_command_is_allowed("reviewer", ["task", "close", "T-0001"])


def test_non_pm_agents_cannot_shape_tasks() -> None:
    assert not agent_command_is_allowed("swe", ["task", "update", "T-0001"])
    assert not agent_command_is_allowed("qa", ["task", "close", "T-0001"])


def test_recovery_agent_keeps_read_only_diagnostics() -> None:
    assert agent_command_is_allowed("recovery", ["pipeline", "journal", "T-0001"])
    assert agent_command_is_allowed("recovery", ["pipeline", "rules"])
    assert agent_command_is_allowed("recovery", ["task", "logs", "T-0001"])


def test_operator_inspection_is_blocked_for_agents() -> None:
    assert not agent_command_is_allowed("planner", ["status"])
    assert not agent_command_is_allowed("recovery", ["task", "show", "T-0001"])
    assert not agent_command_is_allowed("swe", [])


def test_agent_blocked_command_message_names_agent_surface() -> None:
    assert "litehive agent update" in AGENT_BLOCKED_COMMAND_MESSAGE
    assert "operator inspection commands" in AGENT_BLOCKED_COMMAND_MESSAGE
