"""Agent execution boundary.

This package owns subagent invocation mechanics: sandbox preparation, CLI
adapter calls, artifact/session persistence, execution traces, and parsing of
agent outputs. It must not own role prompt policy or lifecycle engine selection;
those live in ``litehive.roles`` and ``litehive.lifecycle`` respectively.
"""
