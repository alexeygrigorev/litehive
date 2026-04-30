from pathlib import Path

from litehive.git.ops import checkpoint_message
from litehive.state.records import create_task


def test_generated_checkpoint_message_uses_task_metadata(tmp_path: Path) -> None:
    task = create_task(
        tmp_path,
        title="Generate detailed commit messages",
        goal="Explain completed work using persisted task metadata.",
        acceptance_criteria=[
            "Subject stays concise",
            "Body includes useful task context",
        ],
    )

    message = checkpoint_message(task)

    assert message.startswith(f"litehive {task.id}: Generate detailed commit messages\n\n")
    assert f"Task: {task.id}" in message
    assert "Title: Generate detailed commit messages" in message
    assert "Goal:\nExplain completed work using persisted task metadata." in message
    assert "Acceptance criteria:\n- Subject stays concise\n- Body includes useful task context" in message
