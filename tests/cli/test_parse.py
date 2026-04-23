from litehive.cli.parse import parse_runner_hooks


def test_parse_runner_hooks_preserves_literal_command_prefixes() -> None:
    hooks = parse_runner_hooks(
        [
            "after_implementing=reject: uv run pytest -q",
            "after_testing=run: echo ok",
        ],
        option_name="--runner-hook",
    )

    assert hooks == {
        "after_implementing": ["reject: uv run pytest -q"],
        "after_testing": ["run: echo ok"],
    }
