"""Lifecycle orchestration boundary.

Lifecycle owns state-machine composition, stage transitions, node execution,
session selection, and Heru engine factory wiring. Agent mechanics stay in
``litehive.agents``; role prompt policy stays in ``litehive.roles``; durable
task/report persistence stays behind ``litehive.tasks`` and ``litehive.state``.
"""
