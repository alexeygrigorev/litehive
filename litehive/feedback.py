"""
Feedback text helpers shared by reports, prompts, and lifecycle code.
"""

FEEDBACK_CAP = 2000
TRUNCATION_MARKER = "\n\n… [truncated — full execution trace in subagent artifacts]"


def cap_feedback(text: str, limit: int = FEEDBACK_CAP) -> str:
    """
    Truncate long subagent feedback for inclusion in a prompt.

    Replacing the tail with ``TRUNCATION_MARKER`` keeps prompts under
    engine context limits while pointing readers (and downstream agents)
    at the full execution-trace artifact.
    """
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


__all__ = [
    "FEEDBACK_CAP",
    "TRUNCATION_MARKER",
    "cap_feedback",
]
