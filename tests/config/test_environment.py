from litehive.config.environment import LitehiveEnvironment


def test_litehive_environment_normalizes_process_values() -> None:
    environment = LitehiveEnvironment.from_mapping(
        {
            "LITEHIVE_AGENT_ROLE": " planner ",
            "LITEHIVE_STAGE": " ",
            "LITEHIVE_SUBAGENT_ID": "SA-0001",
            "LITEHIVE_TASK_ID": " T-0001 ",
            "LITEHIVE_WORKSPACE_ROOT": "/tmp/workspace",
        }
    )

    assert environment.agent_role == "planner"
    assert environment.agent_stage is None
    assert environment.subagent_id == "SA-0001"
    assert environment.task_id == "T-0001"
    assert environment.workspace_root == "/tmp/workspace"
