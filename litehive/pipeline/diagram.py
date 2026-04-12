"""Mermaid diagram generator for the pipeline state machine.

Iterates the ``RULES`` list and produces a ``stateDiagram-v2`` block
that ops / readers can drop into a markdown file or render inline with
Mermaid Live.

Not wired into the CLI yet; importable from tests and scripts. When we
add a ``litehive pipeline graph`` command, it can call
``render_mermaid_diagram(list_transitions())`` directly.
"""

from .transitions import Rule, list_transitions
from .types import TERMINAL_NODES


_STATES_HEADER = "stateDiagram-v2"


def render_mermaid_diagram(rules: list[Rule] | None = None) -> str:
    """Return a Mermaid ``stateDiagram-v2`` string for the given rule set.

    Each rule contributes one ``from --> to : event`` line. Wildcard
    ``frozenset`` ``from_state`` rules expand into one line per member
    state. Callables on ``transition_to`` are rendered as ``<dynamic>``
    (the rule's ``description`` is used as the label instead of the
    event type when set, so the table stays readable).
    """
    rules = rules if rules is not None else list_transitions()
    lines: list[str] = [_STATES_HEADER, "    [*] --> ready"]
    for terminal in sorted(TERMINAL_NODES):
        lines.append(f"    {terminal} --> [*]")
    seen: set[tuple[str, str, str]] = set()
    for rule in rules:
        froms = (
            sorted(rule.from_state)
            if isinstance(rule.from_state, frozenset)
            else [rule.from_state]
        )
        to = rule.transition_to if not callable(rule.transition_to) else "<dynamic>"
        event_name = rule.on_event.__name__
        label = rule.description or event_name
        for frm in froms:
            key = (frm, str(to), label)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"    {frm} --> {to} : {event_name}")
    return "\n".join(lines)


def render_markdown(rules: list[Rule] | None = None) -> str:
    """Return the diagram wrapped in a markdown ```mermaid fence."""
    diagram = render_mermaid_diagram(rules)
    return "```mermaid\n" + diagram + "\n```\n"
