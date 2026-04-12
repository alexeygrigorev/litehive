"""Mermaid diagram generator tests."""

from litehive.pipeline.diagram import render_markdown, render_mermaid_diagram
from litehive.pipeline.transitions import RULES
from litehive.pipeline.types import STAGES, TERMINAL_NODES


def test_mermaid_diagram_contains_all_stage_phases_and_terminals() -> None:
    diagram = render_mermaid_diagram(RULES)
    # Header and terminal arrows
    assert "stateDiagram-v2" in diagram
    assert "[*] --> ready" in diagram
    for terminal in TERMINAL_NODES:
        assert f"{terminal} --> [*]" in diagram
    # Every stage and its before/after phases must appear somewhere
    for stage in STAGES:
        assert stage in diagram
        assert f"before_{stage}" in diagram
        assert f"after_{stage}" in diagram
    # worktree_sync and the two recovery nodes must be there
    assert "worktree_sync" in diagram
    assert "recovering" in diagram
    assert "recovering_pre_exec" in diagram


def test_mermaid_markdown_wraps_diagram_in_fence() -> None:
    md = render_markdown(RULES)
    assert md.startswith("```mermaid\n")
    assert md.rstrip().endswith("```")
    assert "stateDiagram-v2" in md
