from ._base import RoleAgent

INSTRUCTIONS = """\
- You are the QA verifier responsible for focused independent validation.
"""


class QAAgent(RoleAgent):
    """Testing stage: verify the implementation against its acceptance criteria."""

    NODE_NAME = "testing"
    ROLE = "qa"
    INSTRUCTIONS = INSTRUCTIONS
