from pathlib import Path

import pytest

from heru.base import CLIExecutionResult

from litehive.agents.engine_callables import resolve_cli_execution_callable


def test_resolve_cli_execution_callable_returns_bound_method() -> None:
    class Engine:
        def run(self, prompt: str, cwd: Path) -> CLIExecutionResult:
            return CLIExecutionResult(
                adapter="test",
                argv=("test",),
                cwd=cwd,
                exit_code=0,
                stdout=prompt,
                stderr="",
                pid=123,
            )

    run = resolve_cli_execution_callable(Engine(), "run")
    execution = run("hello", Path("/tmp"))

    assert execution.stdout == "hello"


def test_resolve_cli_execution_callable_rejects_missing_method() -> None:
    with pytest.raises(TypeError, match="Engine object has no callable run_live"):
        resolve_cli_execution_callable(object(), "run_live")
